# -*- coding: utf-8 -*-
"""图注修复（统一版）：把历次临时脚本的规则收进一条流水线阶段。

规则（按顺序执行，全部幂等）：
R1 合并切图：同页相邻的两块裁切若属于同一张图（纵向/横向紧贴、投影重叠、尺寸相近）→ 合成一张
R2 剔除文字误检：无注裁图 OCR 出 ≥3 行文字 → 判为「文字块被误切」，删除（仅 B 类书、图数 ≤400）
R3 裸数字注：同页短行「12 标题…」配给最近的图（现代建筑这类版式）
R4 正文内嵌注：正文里的「图N 标题」按图号配给无注图（华夏意匠、建筑：形式这类）
R5 组图复用：同页最近注 → 邻页最近注（只复用「像图注」的注，不复制正文长句）

用法：
    python tools/fix_captions.py <book_dir> [--no-merge] [--no-ocr] [--dry]
"""
import json
import os
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

CJK = re.compile(r"[\u4e00-\u9fff]")
CAPWORD = re.compile(r"(摄|平面|立面|剖面|外观|鸟瞰|示意|构造|故居|村落|宅|楼|堂|寺|塔|桥|院|民居|住宅|照片|图版?|表|版)")
NUM_CAP = re.compile(r"^(\d{1,3})\s*[.、:：]?\s*(\D.{2,45})$")
INLINE_CAP = re.compile(r"图\s*(\d{1,3})(?![\d\-－—])\s*([\u4e00-\u9fffA-Za-z][^。；，,、]{1,28})")

_engine = None


def ocr_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def looks_like_caption(t, strict=True):
    """粗判一段文字像不像图注（用于复用/内嵌注的质量过滤）。"""
    t = re.sub(r"\s+", "", t or "")
    if not (4 <= len(t) <= 40):
        return False
    if not CJK.search(t):
        return False
    if re.match(r"^(摘|表|注|译|编)", t):
        return False
    if t.endswith(("。", "；", "：")):
        return False
    if t.count("，") > 2:
        return False
    if not strict:
        return True
    if t.startswith(("图", "版")) or CAPWORD.search(t):
        return True
    return len(t) <= 16


# ------------------------------------------------------------------ R1 合并切图
def _box(b):
    box = b.get("box") or []
    if len(box) != 4:
        return None
    try:
        return [float(v) for v in box]
    except Exception:
        return None


def _merge_pair(d, a, b):
    """把 b 拼到 a 的画布上，返回新块（图片写 assets），失败返回 None。"""
    from PIL import Image
    ba, bb = _box(a), _box(b)
    if not ba or not bb:
        return None
    if b.get("caption") and a.get("caption") and b["caption"] != a["caption"]:
        return None
    pa = os.path.join(d, a.get("src") or "")
    pb = os.path.join(d, b.get("src") or "")
    if not (os.path.exists(pa) and os.path.exists(pb)):
        return None
    ia, ib = Image.open(pa).convert("RGB"), Image.open(pb).convert("RGB")
    ux0, uy0 = min(ba[0], bb[0]), min(ba[1], bb[1])
    ux1, uy1 = max(ba[2], bb[2]), max(ba[3], bb[3])
    W, H = int(ux1 - ux0), int(uy1 - uy0)
    if W <= 0 or H <= 0 or W * H > 60_000_000:
        return None
    canvas = Image.new("RGB", (W, H), "white")
    canvas.paste(ia, (int(ba[0] - ux0), int(ba[1] - uy0)))
    canvas.paste(ib, (int(bb[0] - ux0), int(bb[1] - uy0)))
    out_id = "fig-m-%s" % str(a.get("id") or "x").replace("fig-", "")
    rel = os.path.join("assets", out_id + ".webp")
    canvas.save(os.path.join(d, rel), "WEBP", quality=88, method=4)
    merged = dict(a)
    merged.update({"id": out_id, "src": rel, "y": int(uy0), "w": W, "h": H, "box": [ux0, uy0, ux1, uy1]})
    return merged


def merge_figures(book, d, gap=26):
    blocks = [b for b in book["blocks"] if b.get("type") == "figure"]
    bypage = {}
    for b in blocks:
        if _box(b):
            bypage.setdefault(b.get("page"), []).append(b)
    merged_n = 0
    for page, figs in bypage.items():
        figs.sort(key=lambda b: (b["box"][1], b["box"][0]))
        changed = True
        while changed:
            changed = False
            for i in range(len(figs)):
                for j in range(i + 1, len(figs)):
                    a, b = figs[i], figs[j]
                    ba, bb = _box(a), _box(b)
                    wa, ha = ba[2] - ba[0], ba[3] - ba[1]
                    wb, hb = bb[2] - bb[0], bb[3] - bb[1]
                    if min(wa, ha, wb, hb) < 30:
                        continue
                    ovx = min(ba[2], bb[2]) - max(ba[0], bb[0])
                    ovy = min(ba[3], bb[3]) - max(ba[1], bb[1])
                    vertical = (abs(bb[1] - ba[3]) <= gap or abs(ba[1] - bb[3]) <= gap) and ovx >= 0.72 * min(wa, wb)
                    horizontal = (abs(bb[0] - ba[2]) <= gap or abs(ba[0] - bb[2]) <= gap) and ovy >= 0.72 * min(ha, hb)
                    if vertical or horizontal:
                        new = _merge_pair(d, a if ba[1] <= bb[1] else b, b if ba[1] <= bb[1] else a)
                        if not new:
                            continue
                        keep, drop = a, b
                        idx_k = book["blocks"].index(keep)
                        book["blocks"][idx_k] = new
                        book["blocks"].remove(drop)
                        figs = [x for x in figs if x is not drop]
                        figs[figs.index(keep) if keep in figs else 0] = new
                        merged_n += 1
                        changed = True
                        break
                if changed:
                    break
    return merged_n


# ------------------------------------------------------------------ R2 文字误检
def text_lines(img_path):
    eng = ocr_engine()
    try:
        res, _ = eng(img_path)
    except Exception:
        return 0
    return len(res or [])


def drop_text_fragments(book, d, budget=400):
    figs = [b for b in book["blocks"] if b.get("type") == "figure" and not b.get("caption")]
    dropped = 0
    for i, b in enumerate(figs):
        if i >= budget:
            break
        p = os.path.join(d, b.get("src") or "")
        if not os.path.exists(p):
            continue
        if text_lines(p) >= 3:
            book["blocks"].remove(b)
            dropped += 1
    return dropped


# ------------------------------------------------------------------ R3/R4/R5
def caption_numbered(book):
    fig_by_page = {}
    for b in book["blocks"]:
        if b.get("type") == "figure":
            fig_by_page.setdefault(b.get("page"), []).append(b)
    n = 0
    for b in book["blocks"]:
        if b.get("type") not in ("paragraph", "heading"):
            continue
        t = (b.get("text") or "").strip()
        if len(t) > 60:
            continue
        m = NUM_CAP.match(t)
        if not m:
            continue
        rest = m.group(2).strip()
        if len(rest) < 2 or not looks_like_caption(rest, False):
            continue
        figs = [f for f in fig_by_page.get(b.get("page"), []) if not f.get("caption")]
        if not figs:
            continue
        near = min(figs, key=lambda f: abs((f.get("y") or 0) - (b.get("y") or 0)))
        if abs((near.get("y") or 0) - (b.get("y") or 0)) > 900:
            continue
        cap = ("%s %s" % (m.group(1), rest))[:60]
        near["caption"] = near["originalCaption"] = near["displayCaption"] = cap
        n += 1
    return n


def caption_inline(book):
    """正文内嵌注：把「图N 标题」的文字按图号配给无注图。"""
    refs = {}
    for b in book["blocks"]:
        if b.get("type") != "paragraph":
            continue
        for m in INLINE_CAP.finditer(b.get("text") or ""):
            num = int(m.group(1))
            title = m.group(2).strip()
            if len(title) < 2 or not looks_like_caption(title, False):
                continue
            if num not in refs or len(title) > len(refs[num]):
                refs[num] = title
    if not refs:
        return 0
    n = 0
    for b in book["blocks"]:
        if b.get("type") != "figure" or b.get("caption"):
            continue
        num = b.get("number")
        if isinstance(num, int) and num in refs:
            cap = ("图%d %s" % (num, refs[num]))[:60]
            b["caption"] = b["originalCaption"] = b["displayCaption"] = cap
            n += 1
    return n


def caption_reuse(book, strict=True):
    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    bypage = {}
    for f in figs:
        bypage.setdefault(f.get("page"), []).append(f)
    n = 0
    for f in [x for x in figs if not x.get("caption")]:
        p, y = f.get("page"), f.get("y") or 0
        same = [c for c in bypage.get(p, []) if c.get("caption") and looks_like_caption(c["caption"], strict)]
        if same:
            near = min(same, key=lambda c: abs((c.get("y") or 0) - y))
            f["caption"] = f["originalCaption"] = f["displayCaption"] = near["caption"]
            n += 1
            continue
        adj = [c for c in (bypage.get(p - 1, []) + bypage.get(p + 1, []))
               if c.get("caption") and looks_like_caption(c["caption"], strict)]
        if adj:
            near = min(adj, key=lambda c: abs((c.get("page") or 0) - p) * 3000 + abs((c.get("y") or 0) - y))
            f["caption"] = f["originalCaption"] = f["displayCaption"] = near["caption"]
            n += 1
    return n



# ------------------------------------------------------------------ R6 短标签注
LABEL_RE = re.compile(r"^[\u4e00-\u9fff]{2,10}图$")


def caption_short_label(book, book_dir):
    """图注在图旁印成短标签（「总平面图」「平面图」「剖面图」）时，按位置配给最近的图。"""
    pages = os.path.join(book_dir, "pages")
    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    if not figs:
        return 0
    bypage = {}
    for f in figs:
        bypage.setdefault(f.get("page"), []).append(f)
    cache = {}
    used = {(f.get("page"), (f.get("caption") or "").strip()) for f in figs if f.get("caption")}
    n = 0
    for f in [x for x in figs if not x.get("caption")]:
        pg = f.get("page")
        y = f.get("y") or 0
        fn = os.path.join(pages, "page_%03d_ocr2.json" % pg)
        if fn not in cache:
            cache[fn] = json.load(open(fn, encoding="utf-8")) if os.path.exists(fn) else []
        cands = []
        for l in cache[fn]:
            t = (l.get("text") or "").strip()
            if not LABEL_RE.match(t) or (pg, t) in used:
                continue
            ly = l.get("y0") or 0
            d = abs(ly - y)
            if d <= 200:
                cands.append((d, t))
        if not cands:
            continue
        cands.sort()
        f["caption"] = f["originalCaption"] = f["displayCaption"] = cands[0][1]
        used.add((pg, cands[0][1]))
        n += 1
    return n



def caption_above(book, book_dir, gap=220):
    """图注印在图上方/下方时按版面位置配对，并删掉被重复当成正文的图注行。"""
    pages_dir = os.path.join(book_dir, "pages")
    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    bypage = {}
    for f in figs:
        bypage.setdefault(f.get("page"), []).append(f)
    cache = {}
    used = {(f.get("page"), (f.get("caption") or "").strip()) for f in figs if f.get("caption")}
    n = 0
    for pg, flist in bypage.items():
        pending = [f for f in flist if not f.get("caption")]
        if not pending:
            continue
        fn = os.path.join(pages_dir, "page_%03d_ocr2.json" % pg)
        if fn not in cache:
            cache[fn] = json.load(open(fn, encoding="utf-8")) if os.path.exists(fn) else []
        lines = cache[fn]
        cands = []
        for f in pending:
            box = f.get("box") or []
            if len(box) >= 4:
                fy_top, fy_bot = box[1], box[3]
                fcx = (box[0] + box[2]) / 2.0
                fw = max(60.0, box[2] - box[0])
            else:
                fy_top = fy_bot = f.get("y") or 0
                fcx, fw = None, 600.0
            for l in lines:
                t = (l.get("text") or "").strip()
                if not (2 <= len(t) <= 36):
                    continue
                if re.search(r"[。，；：？！]", t):
                    continue
                if re.fullmatch(r"[\d\s.．·—\-]+", t):
                    continue
                if re.match(r"^(第\s?\d+[章节讲篇]|目录|图\s?\d|表\s?\d)", t):
                    continue
                if (pg, t) in used:
                    continue
                ly = l.get("y0") or 0
                if fy_top - gap <= ly <= fy_top - 8:
                    dist = fy_top - ly
                elif fy_bot + 4 <= ly <= fy_bot + gap:
                    dist = ly - fy_bot
                else:
                    continue
                if fcx is not None:
                    lcx = ((l.get("x0") or 0) + (l.get("x1") or 0)) / 2.0
                    if abs(lcx - fcx) > max(fw, 700):
                        continue
                cands.append((dist, t, f))
        cands.sort(key=lambda x: x[0])
        taken = set()
        for dist, t, f in cands:
            if f.get("caption") or t in taken or (pg, t) in used:
                continue
            f["caption"] = f["originalCaption"] = f["displayCaption"] = t
            used.add((pg, t))
            taken.add(t)
            n += 1
            for b in list(book["blocks"]):
                if b.get("type") != "paragraph" or b.get("page") != pg:
                    continue
                bt = (b.get("text") or "").strip()
                if bt == t:
                    book["blocks"].remove(b)
                elif bt.endswith(t) and len(bt) > len(t) + 3:
                    b["text"] = bt[:-len(t)].rstrip("，。；：、 ")
    return n


def merge_overlapping(book):
    """同页两块裁图重叠 >50%（同一张图被切成两块）→ 合并，保留有图注的那条的注。"""
    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    bypage = {}
    for f in figs:
        bypage.setdefault(f.get("page"), []).append(f)
    merged = 0
    for pg, fl in bypage.items():
        changed = True
        while changed:
            changed = False
            for i in range(len(fl)):
                for j in range(i + 1, len(fl)):
                    a, b = fl[i], fl[j]
                    ba, bb = a.get("box") or [], b.get("box") or []
                    if len(ba) < 4 or len(bb) < 4:
                        continue
                    ix = max(0, min(ba[2], bb[2]) - max(ba[0], bb[0]))
                    iy = max(0, min(ba[3], bb[3]) - max(ba[1], bb[1]))
                    ai = (ba[2] - ba[0]) * (ba[3] - ba[1])
                    bi = (bb[2] - bb[0]) * (bb[3] - bb[1])
                    if ai <= 0 or bi <= 0:
                        continue
                    if ix * iy < 0.5 * min(ai, bi):
                        continue
                    if a.get("caption") and b.get("caption"):
                        continue   # 两块都有注 → 可能是两张真图，不动
                    keep, drop = (a, b) if ai >= bi else (b, a)
                    if not keep.get("caption") and drop.get("caption"):
                        keep["caption"] = drop["caption"]
                        keep["originalCaption"] = drop.get("originalCaption")
                        keep["displayCaption"] = drop.get("displayCaption")
                    keep["box"] = [min(ba[0], bb[0]), min(ba[1], bb[1]), max(ba[2], bb[2]), max(ba[3], bb[3])]
                    book["blocks"].remove(drop)
                    fl = [x for x in fl if x is not drop]
                    merged += 1
                    changed = True
                    break
                if changed:
                    break
    return merged


def caption_orphan_lines(book, book_dir, reach=340):
    """图注行没被收进任何块（OCR 行存在、块里没有）时，按位置配给最近的无注图。"""
    pages_dir = os.path.join(book_dir, "pages")
    blocks = book["blocks"]
    page_text = {}
    for b in blocks:
        if b.get("type") in ("paragraph", "heading"):
            pg = b.get("page")
            page_text[pg] = page_text.get(pg, "") + (b.get("text") or "")
    figs = [b for b in blocks if b.get("type") == "figure"]
    nocap = [f for f in figs if not (f.get("caption") or "").strip()]
    cache, n = {}, 0
    for f in nocap:
        pg, y = f.get("page"), f.get("y") or 0
        fn = os.path.join(pages_dir, "page_%03d_ocr2.json" % pg)
        if fn not in cache:
            cache[fn] = json.load(open(fn, encoding="utf-8")) if os.path.exists(fn) else []
        best = None
        for l in cache[fn]:
            t = (l.get("text") or "").strip()
            ly = l.get("y0") or 0
            if not t or abs(ly - y) > reach:
                continue
            if t in page_text.get(pg, ""):
                continue
            if not (re.match(r"^图\s?[A-Za-z一二三四五六七八九十\d]", t) or "见[" in t):
                continue
            d = abs(ly - y)
            if best is None or d < best[0]:
                best = (d, t)
        if best:
            cap = re.sub(r"\s+", " ", best[1])[:60]
            f["caption"] = f["originalCaption"] = f["displayCaption"] = cap
            n += 1
    return n


def stats_of(book):
    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    cap = sum(1 for f in figs if f.get("caption"))
    return len(figs), cap


def run(d, do_merge=True, do_ocr=True, dry=False):
    bj = os.path.join(d, "book.json")
    book = json.load(open(bj, encoding="utf-8"))
    f0, c0 = stats_of(book)
    changed = {}
    if do_merge:
        changed["合并切图"] = merge_figures(book, d)
    changed["合并重叠图"] = merge_overlapping(book)
    prof = {}
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pipeline
        prof = pipeline.book_profile(book)
    except Exception:
        prof = {}
    f1, c1 = stats_of(book)
    if do_ocr and "B" in (prof.get("classes") or []) and f1 <= 400:
        changed["剔除文字误检"] = drop_text_fragments(book, d)
    changed["裸数字注"] = caption_numbered(book)
    changed["内嵌注"] = caption_inline(book)
    changed["组图复用"] = caption_reuse(book)
    changed["短标签注"] = caption_short_label(book, d)
    changed["上方注"] = caption_above(book, d)
    changed["孤儿图注行"] = caption_orphan_lines(book, d)
    changed["孤儿图注行(远)"] = caption_orphan_lines(book, d, reach=560)
    f2, c2 = stats_of(book)
    book["stats"]["figures"] = f2
    book["stats"]["paired"] = c2
    out = {"图": (f0, f2), "配注": (c0, c2), "明细": changed}
    if not dry:
        json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return out


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    for d in args:
        if not os.path.join(d, "book.json"):
            continue
        r = run(d, do_merge="--no-merge" not in sys.argv, do_ocr="--no-ocr" not in sys.argv, dry=dry)
        print("== %s 图 %d->%d 配注 %d->%d 明细 %s" % (
            os.path.basename(d)[:40], r["图"][0], r["图"][1], r["配注"][0], r["配注"][1], r["明细"]))


if __name__ == "__main__":
    main()
