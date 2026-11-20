# -*- coding: utf-8 -*-
"""rebuild_text.py —— 用 OCR 缓存 + 页面图重建正文段落顺序（不重 OCR、不重切图、不动 assets）。

针对缺陷：pdf2book 的 parse_page 按 (y, x) 跨栏行序组段，且 figure_regions 用全部文字框遮蔽
（图内标注因此既没进图也没被挡在正文外）→ 双栏/图文混排页出现字符级乱序。

流程（每页）：
  1 读 pages/page_NNN_ocr2.json，去重、去页眉页脚、去纯数字行
  2 渲染页面图（只为算图区，不 OCR）
  3 用「长行」遮蔽算 figure_regions —— 短行不遮蔽，于是图内标注落进图区被剔除
  4 剔除图注/表注行、资料来源行、页眉变体
  5 剩余正文碎片拼整行 → 真双栏时列优先 → 按缩进/间距分段
  6 merge_open：上一段无句末标点且下一段不是新起时并回（否则 gate 的「句中截断」会爆）

用法：
    python tools\rebuild_text.py <书名> scan <起页> <止页>   # 增量，可反复跑
    python tools\rebuild_text.py <书名> report               # 守恒与规模统计
    python tools\rebuild_text.py <书名> apply                # 校验通过后写回（自动备份）
"""
from paths import BOOKS
import io
import json
import os
import re
import shutil
import sys
import time
from collections import Counter

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pdf2book as P
import pypdfium2 as pdfium

BOOKS = BOOKS
HAN = re.compile(r"[\u4e00-\u9fff]")
norm = lambda s: "".join(HAN.findall(s or ""))
btxt = lambda x: x.get("text") or x.get("caption") or ""

OPEN_END = re.compile(r"[。！？；：”』】)）]\s*$")
START_NEW = re.compile(
    r"^\s*(第[\s0-9０-９.．]*[章篇]|第[\s0-9０-９.．]*节|表\s*[0-9０-９]|图\s*[0-9０-９]|"
    r"[0-9]+(\.[0-9]+){1,3}\s|（[0-9]+）|[0-9]+）|[①②③⑤⑥⑦⑧⑨]|【)")


def resolve(arg):
    key = re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", arg or "")
    hit = [d for d in sorted(os.listdir(BOOKS))
           if key and key in re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", d)]
    if len(hit) == 1:
        return os.path.join(BOOKS, hit[0])
    raise SystemExit("书名匹配 %d 本: %s" % (len(hit), hit[:6]))


def head_titles(book_dir):
    """书稿里的章/节标题（本书多数存成 paragraph），用于识别页眉及其 OCR 变体"""
    try:
        bk = json.load(io.open(os.path.join(book_dir, "book.json"), encoding="utf-8"))
    except Exception:
        return []
    out = []
    ish = re.compile(r"^第\s*[0-9０-９.．]*\s*[章篇节]")
    for b in bk.get("blocks", []):
        t = (b.get("text") or "").strip()
        n = norm(t)
        if not n:
            continue
        if b.get("type") == "heading":
            if n.startswith("第") and len(n) >= 5:
                out.append(n)
        elif b.get("type") == "paragraph" and len(n) <= 24 and ish.match(t):
            out.append(n)
    for c in bk.get("chapters", []):
        n = norm(c.get("title") or "")
        if len(n) >= 5:
            out.append(n)
    return sorted(set(out))


def is_head_title(t, titles):
    for h in titles:
        if len(t) < 6 or len(h) < 6:
            continue
        if t[:3] == h[:3] and t[-4:] == h[-4:]:
            return True
        if abs(len(t) - len(h)) <= 2:
            d = sum(1 for a, b2 in zip(t, h) if a != b2) + abs(len(t) - len(h))
            if d <= 2:
                return True
    return False


def load_cache(book_dir):
    pages = os.path.join(book_dir, "pages")
    lbp, lc = {}, Counter()
    for f in sorted(os.listdir(pages)):
        m = re.match(r"page_(\d+)_ocr2\.json", f)
        if not m:
            continue
        p = int(m.group(1))
        L = json.load(io.open(os.path.join(pages, f), encoding="utf-8"))
        lbp[p] = L
        for l in L:
            t = norm(l.get("text", ""))
            if len(t) >= 4:
                lc[t[:8]] += 1
    hdr = {t for t, c in lc.items() if c >= 5}
    pfx = Counter()
    for t in hdr:
        m = re.match("^第[0-9１-９.．]*[章篇节]", t)
        if m:
            pfx[m.group(0)] += 1
    for t in list(hdr):
        m = re.match("^第[0-9１-９.．]*[章篇节]", t)
        if m and pfx[m.group(0)] >= 3:
            hdr.add(t)
    return lbp, hdr


def clean_lines(L, hdr, keep_noise=True):
    """返回 (正文候选行, 噪声行)。噪声=纯数字/字母/单字行（多为表格单元、坐标刻度），
    不再直接丢弃，而是归入标注块，保证数字表格内容不丢。"""
    out, noise = [], []
    for l in sorted(L, key=lambda x: (x["y0"], x["x0"])):
        t = (l.get("text") or "").strip()
        n = norm(t)
        if not t or n[:8] in hdr:
            continue
        rec = {"text": t, "x0": l["x0"], "y0": l["y0"], "x1": l["x1"], "y1": l["y1"]}
        if len(n) < 2 or re.fullmatch(r"[0-9一二三四五六七八九十.．\s~～\-—，,()+%×xX*/=<>≥≤~～]+", t):
            noise.append(rec)
            continue
        if any(o["text"] == t and abs(o["y0"] - l["y0"]) < 12 for o in out[-6:][::-1]):
            continue
        out.append(rec)
    return (out, noise) if keep_noise else out


def stitch(lines, max_gap=70):
    out = []
    for l in sorted(lines, key=lambda x: (x["y0"], x["x0"])):
        placed = False
        for k in range(len(out) - 1, max(-1, len(out) - 6), -1):
            p = out[k]
            h = max(1.0, min(p["y1"] - p["y0"], l["y1"] - l["y0"]))
            ov = min(p["y1"], l["y1"]) - max(p["y0"], l["y0"])
            gap = l["x0"] - p["x1"]
            if ov >= 0.5 * h and -20 <= gap <= max_gap:
                p["text"] += l["text"]
                p["x1"] = max(p["x1"], l["x1"])
                p["y1"] = max(p["y1"], l["y1"])
                placed = True
                break
        if not placed:
            out.append(dict(l))
    return out


def modal_left(ls, unit=12):
    if not ls:
        return 0
    return Counter(int(round(l["x0"] / unit)) for l in ls).most_common(1)[0][0] * unit


def column_major(lines, W, lh):
    rs = sorted(l["x0"] for l in lines if l["x0"] > W * 0.45)
    if len(rs) >= 8:
        cand = rs[len(rs) // 2] - 10
        left = [l for l in lines if l["x0"] < cand]
        right = [l for l in lines if l["x0"] >= cand]
        lc_ = sum(1 for l in left if len(norm(l["text"])) >= 12)
        rc_ = sum(1 for l in right if len(norm(l["text"])) >= 12)
        if lc_ >= 3 and rc_ >= 3:
            return [left, right], cand
    return [lines], None


def paras_from(cl, lh):
    if not cl:
        return []
    cl = sorted(cl, key=lambda x: (x["y0"], x["x0"]))
    base = modal_left(cl)
    out, cur, pb = [], [], None
    for l in cl:
        indented = l["x0"] > base + lh * 1.3
        gapbig = pb is not None and (l["y0"] - pb) > lh * 1.5
        prevtxt = "".join(x["text"] for x in cur)
        if cur and (indented or gapbig):
            if OPEN_END.search(prevtxt) or len(norm(prevtxt)) < 12:
                out.append(prevtxt)
                cur = []
        cur.append(l)
        pb = l["y1"]
    if cur:
        out.append("".join(x["text"] for x in cur))
    return out


def merge_open(paras):
    """上一段无句末标点、下一段不是新起（标题/序号/图表号）时并回去"""
    out = []
    for t in paras:
        if not t.strip():
            continue
        if out:
            prev = out[-1]
            if (not OPEN_END.search(prev)) and len(norm(prev)) >= 8 and not START_NEW.match(t.strip()):
                out[-1] = prev + t
                continue
        out.append(t)
    return out


def scan_page(pdfdoc, img_dir, p, lbp, hdr, titles=None):
    L, noise = clean_lines(lbp.get(p, []), hdr)
    if len(L) < 4:
        return {"page": p, "paras": [], "labels": [], "skipped": "few-lines"}
    png = os.path.join(img_dir, "page_%03d_x3.png" % p)
    if not os.path.exists(png):
        P.render_page(pdfdoc, p, 3.0, img_dir)
    img = Image.open(png).convert("RGB")
    W, H = img.size
    hs = sorted(l["y1"] - l["y0"] for l in L)
    lh = hs[len(hs) // 2] if hs else 40
    long_boxes = [(l["x0"], l["y0"], l["x1"], l["y1"]) for l in L if len(norm(l["text"])) >= 12]
    try:
        fboxes = P.figure_regions(img, long_boxes, lh)
    except Exception:
        fboxes = []

    def inside(l):
        cx, cy = (l["x0"] + l["x1"]) / 2, (l["y0"] + l["y1"]) / 2
        return any(x0 - 6 <= cx <= x1 + 6 and y0 - 6 <= cy <= y1 + 6 for (x0, y0, x1, y1) in fboxes)

    CAPLINE = re.compile(r"^\s*[（(]?[上下左右]?[：:]?\s*(图|表)\s*[0-9０-９]+([.．\-—~～][0-9０-９]+)*")
    SRC_LINE = re.compile(r"^\s*(资料来源|来源[：:]|引自)")

    PREDICATE = re.compile(r"(可知|表明|所示|反映|说明|表示|见图|见表|即|因此|以及|其中|可以|常用|等于|称为|为|是)")

    def is_caption(l):
        t = (l.get("text") or "").strip()
        if SRC_LINE.match(t) and len(norm(t)) <= 45:
            return True
        if not CAPLINE.match(t) or len(norm(t)) > 40:
            return False
        # 带句末标点、或图号后面就跟谓语的，是正文句子（如「图1.2-18是说明…」），不隔离
        if re.search(r"[。！？]", t):
            return False
        m = CAPLINE.match(t)
        tail = t[m.end():]
        if PREDICATE.match(tail.strip()):
            return False
        return True

    def is_head_frag(l):
        return bool(titles) and is_head_title(norm(l["text"]), titles)

    body_raw = [l for l in L if not inside(l) and not is_caption(l) and not is_head_frag(l)]
    labels_raw = [l for l in L if inside(l) or is_caption(l) or is_head_frag(l)] + noise

    lines = stitch(body_raw)
    cols, g = column_major(lines, W, lh)
    paras = []
    for c in cols:
        paras += paras_from(c, lh)
    paras = merge_open(paras)
    return {"page": p, "paras": paras, "labels": [l["text"] for l in labels_raw],
            "gutter": g, "lh": lh, "figs": len(fboxes)}


def find_pdf(book_dir):
    name = os.path.basename(book_dir)
    for base in [os.path.join(ROOT, "inbox"), os.path.join(ROOT, "pending")]:
        c = os.path.join(base, name + ".pdf")
        if os.path.exists(c):
            return c
    bk = json.load(io.open(os.path.join(book_dir, "book.json"), encoding="utf-8"))
    src = os.path.basename(bk.get("source") or "")
    for base in [os.path.join(ROOT, "inbox"), os.path.join(ROOT, "pending")]:
        c = os.path.join(base, src)
        if os.path.exists(c):
            return c
    raise SystemExit("找不到原 PDF")


def scan(book_dir, lo, hi):
    out_json = os.path.join(book_dir, "_rebuild_scan.json")
    data = json.load(io.open(out_json, encoding="utf-8")) if os.path.exists(out_json) else {}
    lbp, hdr = load_cache(book_dir)
    titles = head_titles(book_dir)
    doc = pdfium.PdfDocument(find_pdf(book_dir))
    img_dir = os.path.join(book_dir, "_rebuild_imgs")
    os.makedirs(img_dir, exist_ok=True)
    t0 = time.time()
    for p in range(lo, hi + 1):
        if p not in lbp or str(p) in data:
            continue
        data[str(p)] = scan_page(doc, img_dir, p, lbp, hdr, titles)
        if (p - lo) % 25 == 0:
            json.dump(data, io.open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
            print("  ... p%d 累计%.0fs" % (p, time.time() - t0))
    json.dump(data, io.open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    print("scan 完成：页 %d，用时 %.0fs" % (len(data), time.time() - t0))


def report(book_dir):
    data = json.load(io.open(os.path.join(book_dir, "_rebuild_scan.json"), encoding="utf-8"))
    book = json.load(io.open(os.path.join(book_dir, "book.json"), encoding="utf-8"))
    bl = book["blocks"]
    old = Counter(norm("".join(btxt(b) for b in bl if b.get("type") in ("paragraph", "heading"))))
    np_, nl_ = 0, 0
    tot = Counter()
    for v in data.values():
        for t in v.get("paras", []):
            tot.update(norm(t)); np_ += 1
        for t in v.get("labels", []):
            tot.update(norm(t)); nl_ += 1
    lost = sum((old - tot).values())
    print("页 %d | 新正文段 %d | 新标签行 %d" % (len(data), np_, nl_))
    print("旧 %d 字 -> 新 %d 字 | 缺 %d (%.2f%%)" % (sum(old.values()), sum(tot.values()), lost,
          100.0 * lost / max(1, sum(old.values()))))
    print("旧段落块 %d" % sum(1 for b in bl if b.get("type") == "paragraph"))


def apply(book_dir):
    data = json.load(io.open(os.path.join(book_dir, "_rebuild_scan.json"), encoding="utf-8"))
    fp = os.path.join(book_dir, "book.json")
    book = json.load(io.open(fp, encoding="utf-8"))
    bl = book["blocks"]
    # 守恒口径用「原始字符」（含数字/字母），能抓到表格数字被丢的情况
    raw = lambda s: re.sub(r"\s", "", s or "")
    old = Counter(raw("".join(btxt(b) for b in bl if b.get("type") in ("paragraph", "heading"))))
    tot = Counter()
    for v in data.values():
        for t in v.get("paras", []) + v.get("labels", []):
            tot.update(raw(t))
    lost = sum((old - tot).values())
    rate = 100.0 * lost / max(1, sum(old.values()))
    print("守恒（原始字符口径）：缺 %d 字符 (%.2f%%)" % (lost, rate))
    if rate > 2.0:
        print("!! 缺失超过 2%，拒绝写回")
        return 1
    bak = os.path.join(book_dir, "book.json.bak_%s_正文重建前" % time.strftime("%Y%m%d_%H%M"))
    shutil.copy2(fp, bak)
    by_page = {}
    for i, b in enumerate(bl):
        by_page.setdefault(b.get("page"), []).append(i)
    data_pages = {int(k) for k, v in data.items() if v.get("paras")}
    maxp = max([p for p in by_page] + list(data_pages)) if (by_page or data_pages) else 0
    new_bl = []
    for p in range(1, maxp + 1):
        idxs = by_page.get(p, [])
        v = data.get(str(p))
        if not idxs:
            # 零块页：书稿该页一个字都没有、但 OCR 有正文 → 按扫描结果新建段落块（回填真丢正文）
            if v and v.get("paras"):
                labs = [t for t in v.get("labels", []) if t.strip()]
                new_bl += [{"type": "paragraph", "page": p, "y": 200 + n, "col": 0, "indent": 1, "text": t}
                           for n, t in enumerate(v["paras"]) if t.strip()]
                if labs:
                    new_bl.append({"type": "paragraph", "page": p, "y": 9999, "col": 1, "indent": 0,
                                   "text": "（图内标注/表格文字：" + "".join(labs) + "）"})
            continue
        if not v or not v.get("paras"):
            new_bl.extend(bl[i] for i in idxs)
            continue
        newp = [{"type": "paragraph", "page": p, "y": 200 + n, "col": 0, "indent": 1, "text": t}
                for n, t in enumerate(v["paras"]) if t.strip()]
        labs = [t for t in v.get("labels", []) if t.strip()]
        lab_blk = [{"type": "paragraph", "page": p, "y": 9999, "col": 1, "indent": 0,
                    "text": "（图内标注/表格文字：" + "".join(labs) + "）"}] if labs else []
        done_p = False
        for i in idxs:
            b = bl[i]
            if b.get("type") == "paragraph":
                if not done_p:
                    new_bl += newp + lab_blk
                    done_p = True
                continue
            new_bl.append(b)
        if not done_p:
            new_bl += newp + lab_blk
    book["blocks"] = new_bl
    book["stats"]["chars"] = sum(len(btxt(b)) for b in new_bl
                                 if b.get("type") in ("paragraph", "heading", "table"))
    book["stats"]["paragraphs"] = sum(1 for b in new_bl if b.get("type") == "paragraph")
    tmp = fp + ".tmp"
    json.dump(book, io.open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    os.replace(tmp, fp)
    print("已写回：blocks %d -> %d | chars %d | 备份 %s"
          % (len(bl), len(new_bl), book["stats"]["chars"], os.path.basename(bak)))
    return 0


if __name__ == "__main__":
    cmd = sys.argv[2] if len(sys.argv) > 2 else "report"
    d = resolve(sys.argv[1])
    if cmd == "scan":
        scan(d, int(sys.argv[3]), int(sys.argv[4]))
    elif cmd == "apply":
        sys.exit(apply(d) or 0)
    else:
        report(d)
