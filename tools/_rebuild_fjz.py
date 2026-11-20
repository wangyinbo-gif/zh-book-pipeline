# -*- coding: utf-8 -*-
"""《中国古代建筑史（第二版）》正文/标题重建（不重 OCR、不重切图、不动 assets）。

根因：pdf2book.parse_page 按 (y,x) 组段 + 页眉当 heading + 图内标注混入正文，
导致：整页正文并进相邻页段落（页归属错位）、图注粘段、页眉标题块 292 个、图内标注进正文。

做法（每页）：
 1 读 pages/page_NNN_ocr2.json，去掉跨页重复的页眉行（仅在页面顶部带）、页码、纯数字噪声
 2 同 y 相邻碎行先拼回整行（附录注释 [N] + 文字这种）
 3 图注行（^图N）单独收集；页内图区（遮掉全部文字框后按墨迹/方差找连通域）内的小字 = 图内标注，剔出正文
 4 用"长行"确定正文栏 x 基线；x0 落不到任何栏的短行 = 图内标注/图版文字，剔出正文
 5 栏内按 y 排序 → 按段首缩进/行距分段
 6 标题不按 block 猜，而是按书前目录（p5-6）的规范标题做"顺序匹配"，在 OCR 里找每个章/节标题的首次出现页

用法：
  python tools\\_rebuild_fjz.py scan [起页] [止页]     # 默认全书；写 _工作记录/古建史与全库审查/_rebuild_scan.json
  python tools\\_rebuild_fjz.py report                # 守恒/逐页对比
  python tools\\_rebuild_fjz.py apply --dry           # 只算不写
  python tools\\_rebuild_fjz.py apply                 # 写回 book.json（自动备份）
"""
from paths import TOOLS
import io
import json
import os
import re
import shutil
import sys
import time
from collections import Counter

sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(TOOLS, "..", "_工作记录", "古建史与全库审查"))
import common as C

import numpy as np
from PIL import Image

sys.path.insert(0, TOOLS)
import pdf2book as P

REVIEW = C.REVIEW
BOOK = C.BOOK_DIR
SCAN = os.path.join(REVIEW, "_rebuild_scan.json")
FIGPLAN = os.path.join(REVIEW, "figures", "caption_plan.json")
RENDER3 = os.path.join(REVIEW, "_render3")

HAN = re.compile(r"[\u4e00-\u9fff]")
NUMLINE = re.compile(r"^[0-9０-９一二三四五六七八九十.．:：\-—~～，,]+$")
TERM = "。！？…：；”』」）)】.．!?：;》"
CAP_RE = re.compile(r"^[（(]?[上下左右]?[：:]?\s*图\s*[0-9０-９]+")

# 书前目录（pages 5-6 OCR 人工校对后的规范标题，含章/节/附录，按书内顺序）
TOC = [
    ("chapter", "说明"),
    ("chapter", "绪论"),
    ("section", "第一节 自然条件对中国古代建筑的影响"),
    ("section", "第二节 中国古代建筑发展的几个阶段"),
    ("section", "第三节 中国古代建筑的特点"),
    ("chapter", "第一章 原始社会时期的建筑遗迹"),
    ("section", "第一节 原始人群的住所"),
    ("section", "第二节 仰韶文化的建筑遗迹"),
    ("section", "第三节 龙山文化的建筑遗迹"),
    ("chapter", "第二章 夏、商、西周、春秋时期的建筑"),
    ("section", "第一节 夏—中国奴隶制国家的诞生"),
    ("section", "第二节 商朝的宫室和陵墓"),
    ("section", "第三节 西周和春秋时代的建筑"),
    ("chapter", "第三章 战国、秦、两汉、三国时期的建筑"),
    ("section", "第一节 战国到三国时期社会的变动和建筑概况"),
    ("section", "第二节 城市的发展"),
    ("section", "第三节 秦、汉、三国的宫室"),
    ("section", "第四节 住宅"),
    ("section", "第五节 陵墓"),
    ("section", "第六节 秦万里长城和汉长城"),
    ("section", "第七节 建筑的材料、技术和艺术"),
    ("chapter", "第四章 两晋、南北朝时期的建筑"),
    ("section", "第一节 两晋、南北朝时期社会的变动和建筑概况"),
    ("section", "第二节 都城及宫殿"),
    ("section", "第三节 住宅"),
    ("section", "第四节 寺和塔"),
    ("section", "第五节 石窟的建筑和雕刻"),
    ("section", "第六节 陵墓"),
    ("section", "第七节 建筑的材料、技术和艺术"),
    ("chapter", "第五章 隋、唐、五代时期的建筑"),
    ("section", "第一节 隋、唐、五代时期社会的变动和建筑概况"),
    ("section", "第二节 隋、唐的都城与宫殿"),
    ("section", "第三节 住宅"),
    ("section", "第四节 寺、塔、石窟"),
    ("section", "第五节 陵墓"),
    ("section", "第六节 安济桥"),
    ("section", "第七节 建筑的材料、技术和艺术"),
    ("chapter", "第六章 宋、辽、金时期的建筑"),
    ("section", "第一节 宋、辽、金时期社会的变动和建筑概况"),
    ("section", "第二节 城市与宫殿"),
    ("section", "第三节 住宅"),
    ("section", "第四节 祠庙及寺、塔、经幢"),
    ("section", "第五节 陵墓"),
    ("section", "第六节 《营造法式》"),
    ("section", "第七节 建筑的材料、技术和艺术"),
    ("chapter", "第七章 元、明、清时期的建筑"),
    ("section", "第一节 元朝社会的变动和建筑概况"),
    ("section", "第二节 元大都和大都宫殿"),
    ("section", "第三节 元朝的宗教建筑"),
    ("section", "第四节 明、清时期社会的变动和建筑概况"),
    ("section", "第五节 明、清的都城及宫苑"),
    ("section", "第六节 明代的万里长城和海防据点"),
    ("section", "第七节 明、清一般城镇、住宅、园林及家具陈设"),
    ("section", "第八节 明、清的坛庙建筑和陵墓建筑"),
    ("section", "第九节 明、清的宗教建筑"),
    ("section", "第十节 元、明、清建筑的材料、技术和艺术"),
    ("appendix", "附录一 注释"),
    ("appendix", "附录三 历代尺度简表"),
]
# 原书目录有「附录二 中国历史年表」，但扫描本缺该页（p420-426 全是附录一注释，p427 直接附录三），
# 不插空标题，只在审查报告登记。
EXTRA_HEADINGS = [("appendix", "《中国古代建筑史》的编写过程")]  # p428 后记，目录未列
SKIP_PAGES = {1, 2, 3, 5, 6}   # 封面/书名/版权/目录页：不进正文（目录由阅读器 TOC 提供）


def norm(s):
    """规范化：只留汉字与数字，去空格标点，用于标题匹配。"""
    return re.sub(r"[^\u4e00-\u9fff0-9]", "", s or "")


def cjk_len(s):
    return len(HAN.findall(s or ""))


def load_ocr_all():
    return {p: C.load_ocr(p) for p in range(1, 430)}


def header_set(allocr):
    """跨页重复≥5 次的行 → 页眉候选（只按文本判定，删的时候再看位置）。"""
    c = Counter()
    for L in allocr.values():
        for l in L:
            n = norm(l["text"])
            if len(n) >= 4:
                c[n[:8]] += 1
    return {t for t, n in c.items() if n >= 5}


def render3(p):
    os.makedirs(RENDER3, exist_ok=True)
    out = os.path.join(RENDER3, "page_%03d.png" % p)
    if os.path.exists(out):
        return out
    import pymupdf
    doc = pymupdf.open(C.PDF)
    doc[p - 1].get_pixmap(matrix=pymupdf.Matrix(3.0, 3.0)).save(out)
    return out


def figure_boxes(p, lines):
    """遮掉全部文字框后找图区（照片/线图）。返回 [(x0,y0,x1,y1)]，坐标与 ocr2 同尺度。"""
    try:
        img = Image.open(render3(p)).convert("RGB")
        W, H = img.size
        hh = sorted(l["y1"] - l["y0"] for l in lines) or [40]
        lh = hh[len(hh) // 2]
        boxes = [(l["x0"] - 3, l["y0"] - 3, l["x1"] + 3, l["y1"] + 3) for l in lines]
        return P.figure_regions(img, boxes, lh), W, H, lh
    except Exception as e:
        print("  !! p%d figure_boxes: %s" % (p, e))
        return [], 2214, 3200, 55


def join_overlap(a, b):
    """拼接两个 OCR 文字框：去掉接缝处 1~4 字的重复（「在塔」+「塔身」→「在塔身」）。"""
    for k in range(min(3, len(a), len(b)), 0, -1):
        if a.endswith(b[:k]):
            return a + b[k:]
    return a + b


def merge_same_line(lines):
    """同 y 相邻碎行拼整行（附录 [123]+文字；标题 第N节+标题；OCR 把一整行切成左右两半）。"""
    out = []
    for l in sorted(lines, key=lambda x: (x["y0"], x["x0"])):
        placed = False
        for k in range(len(out) - 1, max(-1, len(out) - 5), -1):
            p = out[k]
            h = max(1.0, min(p["y1"] - p["y0"], l["y1"] - l["y0"]))
            ov = min(p["y1"], l["y1"]) - max(p["y0"], l["y0"])
            if ov < 0.55 * h:
                continue
            short = cjk_len(p["text"]) <= 10 and cjk_len(l["text"]) <= 10
            limit = 260 if short else 160
            if l["x0"] >= p["x0"]:      # 新块在右
                gap = l["x0"] - p["x1"]
                if -90 <= gap <= limit:
                    p["text"] = join_overlap(p["text"], l["text"])
                    p["x1"] = max(p["x1"], l["x1"])
                    p["y1"] = max(p["y1"], l["y1"])
                    placed = True
                    break
            else:                        # 新块在左（OCR 顺序颠倒）
                gap = p["x0"] - l["x1"]
                if -90 <= gap <= limit:
                    p["text"] = join_overlap(l["text"], p["text"])
                    p["x0"] = l["x0"]
                    p["y0"] = min(p["y0"], l["y0"])
                    p["y1"] = max(p["y1"], l["y1"])
                    placed = True
                    break
        if not placed:
            out.append(dict(l))
    return out


def fuzzy_title_match(text, title, max_diff=None):
    a, b = norm(text), norm(title)
    if not a or not b:
        return False
    if a == b:
        return True
    if len(b) < 4:
        return False
    if a in b or b in a:
        return abs(len(a) - len(b)) <= 2
    if abs(len(a) - len(b)) > 3:
        return False
    d = sum(1 for x, y in zip(a, b) if x != y) + abs(len(a) - len(b))
    return d <= (max_diff if max_diff is not None else max(2, int(len(b) * 0.12)))


def classify_page(p, L, hdr, W, H, lh, fboxes, want_headings=True):
    """一页 → dict(paras=[{text,y}], headings=[{text,y}], captions=[{text,y}], labels=[...])"""
    HDRBAND = H * 0.115
    TOCN = [(t, norm(t)) for _, t in (TOC + EXTRA_HEADINGS)]

    def hit_toc(t):
        n = norm(t)
        if n in ("绪论", "说明"):
            return n
        m = re.fullmatch(r"(附录[一二三])", n)
        if m:
            for title, tn in TOCN:
                if tn.startswith(m.group(1)):
                    return title
        if len(n) < 4:
            return None
        best = None
        for title, tn in TOCN:
            s = title_score(t, title)
            if s >= 0.8 and (best is None or s > best[0]):
                best = (s, title)
        return best[1] if best else None

    # 1) 去页眉/页码/水印；页眉带内命中目录标题的行按"标题候选"收走（页眉或真标题），不进正文
    lines, top_heads = [], []
    for l in L:
        t = (l["text"] or "").strip()
        if not t:
            continue
        if NUMLINE.match(t) and (l["y0"] < HDRBAND or l["y0"] > H * 0.90):
            continue
        rec = {"text": t, "x0": l["x0"], "y0": l["y0"], "x1": l["x1"], "y1": l["y1"]}
        if l["y0"] < HDRBAND:
            t2 = re.sub(r"^[0-9]{1,4}\s*", "", t) if re.match(r"^[0-9]{1,4}", t) else t
            if t2 != t and (hit_toc(t2) or norm(t2)[:8] in hdr):
                continue
            h = hit_toc(t)
            if h:
                top_heads.append({"text": t, "y": l["y0"], "x": l["x0"], "canon": h})
                continue
            if norm(t)[:8] in hdr:
                continue
            if (cjk_len(t) <= 3 or re.match(r"^(第[0-9一二三四五六七八九十]+[章节]|绪论|附录|目录|"
                                            r"中国古代建筑史|责任编辑|（京）|ISBN)", t)):
                continue
        lines.append(rec)
    lines = merge_same_line(lines)

    # 2) 图注（含续行）
    caps, rest = [], []
    for i, l in enumerate(lines):
        if CAP_RE.match(l["text"]) and cjk_len(l["text"]) >= 1:
            caps.append({"text": l["text"], "y": l["y0"], "x": l["x0"]})
        else:
            rest.append(l)
    # 图注续行：紧跟图注下方、左端接近、短
    rest2 = []
    for l in rest:
        joined = False
        for c in caps:
            if (0 < l["y0"] - c["y"] <= lh * 1.6 and abs(l["x0"] - c["x"]) <= 140
                    and cjk_len(l["text"]) <= 26 and not CAP_RE.match(l["text"])):
                c["text"] += l["text"]
                joined = True
                break
        if not joined:
            rest2.append(l)
    rest = rest2

    # 3) 图区（图内标注）
    def inside(l):
        cx, cy = (l["x0"] + l["x1"]) / 2, (l["y0"] + l["y1"]) / 2
        return any(x0 - 8 <= cx <= x1 + 8 and y0 - 8 <= cy <= y1 + 8 for (x0, y0, x1, y1) in fboxes)

    # 4) 标题候选：正文行里命中目录标题的，收进 headings 并从正文剔除
    heads = list(top_heads)
    body_seed = []
    for l in rest:
        h = hit_toc(l["text"]) if want_headings else None
        if h and not inside(l):
            heads.append({"text": l["text"], "y": l["y0"], "x": l["x0"], "canon": h})
        else:
            body_seed.append(l)
    rest = body_seed

    # 5) 正文栏基线：用"长行"（汉字>=10 且不在图区）聚类 x0
    seeds = [l for l in rest if cjk_len(l["text"]) >= 10 and not inside(l)]
    bases = []
    for x in sorted(l["x0"] for l in seeds):
        if bases and x - bases[-1][-1] < 150:
            bases[-1].append(x)
        else:
            bases.append([x])
    clusters = [(int(np.median(b)), len(b)) for b in bases]
    if clusters:
        big = max(clusters, key=lambda c: c[1])
        clusters = [c for c in clusters if c[1] >= 3 or abs(c[0] - big[0]) < 150]
    bases = [c[0] for c in clusters] or [min((l["x0"] for l in rest), default=0)]

    labels, body = [], []
    for l in rest:
        if inside(l):
            labels.append(l)
            continue
        base = min(bases, key=lambda b: abs(l["x0"] - b))
        if not (base - 80 <= l["x0"] <= base + lh * 2.6):
            # 不在任何正文栏：短行算标注；长行仍可能是正文（栏基线被图挤偏时兜底）
            if cjk_len(l["text"]) < 10:
                labels.append(l)
                continue
        body.append({"text": l["text"], "x0": l["x0"], "y0": l["y0"], "x1": l["x1"],
                     "y1": l["y1"], "base": base})

    # 5b) 标题已在上一步收进 heads（含页眉带），正文里不再重复
    headings = heads

    # 6) 分段：先定阅读顺序，再按「段首缩进 / 大行距 / 注释条目」断段
    def paras_of(lines):
        if not lines:
            return []
        out, cur, prev = [], [], None
        for l in lines:
            indented = l["x0"] > l["base"] + lh * 0.9
            gapbig = prev is not None and (l["y0"] - prev) > lh * 1.55
            newentry = bool(re.match(r"^\[\d+\]", l["text"]))  # 注释/参考文献条目
            prevtxt = "".join(x["text"] for x in cur)
            if cur and (indented or newentry or (gapbig and (not prevtxt or prevtxt[-1] in TERM))):
                out.append((prevtxt, cur[0]["y0"]))
                cur = []
            cur.append(l)
            prev = l["y1"]
        if cur:
            out.append(("".join(x["text"] for x in cur), cur[0]["y0"]))
        return out

    # 多栏判定：两个基线各自的 y 跨度若大面积重叠 = 真左右分栏；否则是「绕图排版」，按 y 通排
    mode_2col = False
    if len(bases) > 1:
        spans = {}
        for b in bases:
            cl = [l for l in body if l["base"] == b]
            if cl:
                spans[b] = (min(l["y0"] for l in cl), max(l["y1"] for l in cl))
        if len(spans) >= 2:
            (b1, (a1, a2)), (b2, (c1, c2)) = sorted(spans.items())[:2]
            lo, hi = max(a1, c1), min(a2, c2)
            mode_2col = (hi - lo) > H * 0.30
    if mode_2col:
        body.sort(key=lambda l: (bases.index(l["base"]), l["y0"], l["x0"]))
    else:
        body.sort(key=lambda l: (l["y0"], l["x0"]))
    pairs = paras_of(body)
    paras = [{"text": t, "y": y} for t, y in pairs]

    return {"paras": paras, "headings": headings, "captions": caps,
            "labels": [l["text"] for l in labels], "bases": bases,
            "figs": len(fboxes), "twoCol": mode_2col}


def augment_lines(p, cache_lines):
    """条带重 OCR 补行：只把缓存里完全没有的行（y 不重叠）补进来，不动原有文本。"""
    fp = os.path.join(REVIEW, "_reocr", "page_%03d.json" % p)
    if not os.path.exists(fp):
        return cache_lines
    try:
        extra_all = json.load(io.open(fp, encoding="utf-8"))
    except Exception:
        return cache_lines
    out = list(cache_lines)
    added = 0
    for l in extra_all:
        h = max(1.0, l["y1"] - l["y0"])
        if any(min(l["y1"], c["y1"]) - max(l["y0"], c["y0"]) > 0.45 * h for c in cache_lines):
            continue
        out.append(l)
        added += 1
    if added:
        print("  +p%d 补行 %d" % (p, added))
    return out


def scan(lo=1, hi=429):
    allocr = load_ocr_all()
    hdr = header_set(allocr)
    data = json.load(io.open(SCAN, encoding="utf-8")) if os.path.exists(SCAN) else {}
    t0 = time.time()
    for p in range(lo, hi + 1):
        if str(p) in data:
            continue
        L = allocr[p]
        L = augment_lines(p, L)
        fboxes, W, H, lh = figure_boxes(p, L)
        r = classify_page(p, L, hdr, W, H, lh, fboxes)
        r["page"] = p
        r["W"], r["H"], r["lh"] = W, H, round(lh, 1)
        data[str(p)] = r
        if p % 20 == 0:
            json.dump(data, io.open(SCAN, "w", encoding="utf-8"), ensure_ascii=False)
            print("  ... p%d  累计 %.0fs" % (p, time.time() - t0), flush=True)
    json.dump(data, io.open(SCAN, "w", encoding="utf-8"), ensure_ascii=False)
    print("scan 完成：%d 页，用时 %.0fs" % (len(data), time.time() - t0))


CN = "零一二三四五六七八九十"


def cn_num(s):
    """『第三』/『十二』→ 3/12。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + (CN.index(s[1]) if len(s) > 1 and s[1] in CN else 0)
    if "十" in s:
        a, b = s.split("十", 1)
        return CN.index(a) * 10 + (CN.index(b) if b in CN else 0)
    return CN.index(s) if s in CN else None


def parse_title(title):
    """→ (kind, num)  kind ∈ chapter/section/appendix/intro"""
    n = norm(title)
    m = re.match(r"^第([一二三四五六七八九十]+)章", n)
    if m:
        return "chapter", cn_num(m.group(1))
    m = re.match(r"^第([一二三四五六七八九十]+)节", n)
    if m:
        return "section", cn_num(m.group(1))
    m = re.match(r"^附录([一二三四五六七八九十]+)", n)
    if m:
        return "appendix", cn_num(m.group(1))
    return "intro", None


def match_headings(scan, allocr=None):
    """按 TOC 顺序在全书找每个标题的首次出现页：标题的『第N章/第N节/附录N』必须一致，
    再比文字相似度，取该页最像的一条。TOC 页（p≤6）不参与。返回按 TOC 顺序的列表。"""
    found = []
    last_page = 0
    use = TOC + EXTRA_HEADINGS
    for kind, title in use:
        tk, tnum = parse_title(title)
        hit = None
        for p in range(max(1, last_page), 430):
            if p in (5, 6):     # 书前目录页不参与（目录条目不是正文标题）
                continue
            r = scan.get(str(p))
            if not r:
                continue
            best = None
            for h in r["headings"]:
                hk, hnum = parse_title(h["text"])
                if hk != tk or hnum != tnum:
                    continue
                # 附录只认编号（标题文字 OCR 常只读到「附录三」）
                if tk == "appendix":
                    score = 1.0
                else:
                    score = title_score(h["text"], title)
                if score >= 0.55 and (best is None or score > best[0]):
                    best = (score, h["y"], h["text"])
            if best:
                hit = (p, best[1], best[2], best[0])
                break
        found.append({"kind": kind, "title": title,
                      "page": hit[0] if hit else None,
                      "y": hit[1] if hit else None,
                      "ocr": hit[2] if hit else None,
                      "score": round(hit[3], 2) if hit else 0})
        if hit:
            last_page = hit[0]
    return found


def title_score(a, b):
    """0~1 相似度：完全相同 1.0；按编辑距离折算。"""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 1.0 - abs(len(na) - len(nb)) / max(len(na), len(nb))
    # 去掉共同的「第N章/节/附录N」前缀再比
    ka, kb = parse_title(a), parse_title(b)
    if ka != kb:
        return 0.0
    m = re.match(r"^(第[一二三四五六七八九十]+[章节]|附录[一二三四五六七八九十]+)", na)
    if m:
        na = na[m.end():]
    m = re.match(r"^(第[一二三四五六七八九十]+[章节]|附录[一二三四五六七八九十]+)", nb)
    if m:
        nb = nb[m.end():]
    if not na or not nb:
        return 0.6
    d = sum(1 for x, y in zip(na, nb) if x != y) + abs(len(na) - len(nb))
    return max(0.0, 1.0 - d / max(len(na), len(nb)))


def report():
    scan_data = json.load(io.open(SCAN, encoding="utf-8"))
    BEFORE = os.path.join(REVIEW, "book_before.json")
    book = C.load_book()
    oldbook = json.load(io.open(BEFORE, encoding="utf-8")) if os.path.exists(BEFORE) else book
    old = Counter()
    for b in book["blocks"]:
        if b["type"] in ("paragraph", "heading"):
            old.update(C.norm(b.get("text") or ""))
    new = Counter()
    labels = Counter()
    npara = 0
    for p, r in scan_data.items():
        if int(p) in SKIP_PAGES:
            continue
        for t in r["paras"]:
            new.update(C.norm(t["text"]))
            npara += 1
        for t in r["labels"]:
            labels.update(C.norm(t))
    missing = old - new
    extra = new - old
    print("旧正文汉字 %d → 新正文汉字 %d（段 %d）" % (sum(old.values()), sum(new.values()), npara))
    print("旧有新无 %d 字；新有旧无 %d 字；图内标注剔出 %d 字" %
          (sum(missing.values()), sum(extra.values()), sum(labels.values())))
    print("旧有新无里被『图内标注』覆盖 %d 字" % sum((missing & labels).values()))
    print("仍无法解释的缺失 %d 字" % sum((missing - labels).values()))
    hf = match_headings(scan_data)
    miss = [v["title"] for v in hf if not v["page"]]
    print("标题匹配：%d/%d，未找到：%s" % (len(hf) - len(miss), len(hf), miss))
    for v in hf:
        print("  %-8s p%-4s %-28s <- %s" % (v["kind"], v["page"], v["title"], (v["ocr"] or "")[:26]))


def clean_caption(t):
    """判断一行 OCR 是否像图注（排除正文里「（图74）。」这类引用）。"""
    s = (t or "").strip()
    if not CAP_RE.match(s):
        return False
    n = cjk_len(s)
    if n < 3 and not re.match(r"^图\s*\d+(?:[.\-—~～]\d+)?$", norm(s) and s):
        return False
    if re.search(r"[，。；：、]$", s):
        return False
    if n < 5 and re.search(r"[）)]$", s):
        return False
    return True


def cap_number(t):
    m = re.match(r"^\s*[（(]?\s*图\s*([0-9０-９]+)(?:[.．\-—~～]([0-9０-９]+))?", t or "")
    if not m:
        return None
    a = int(m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
    b = m.group(2)
    return (a, int(b) if b else 0)


FIG_LEAD = re.compile(r"^图\s*[0-9０-９]+(?:\s*[-—－.．~～]\s*[0-9０-９]+)?\s*[、,，.．:：·]?\s*")


def fix_caption(b):
    """图注文本收尾：去首尾空白、合并多余空格、去掉句末页码残留。"""
    cap = (b.get("caption") or "").strip()
    if not cap:
        return ""
    cap = re.sub(r"\s+", " ", cap)
    cap = re.sub(r"\s*[0-9]{1,3}\s*$", "", cap) if not re.search(r"[0-9]$", cap[:-3] or "") else cap
    return cap.strip()


def fix_display(b):
    """重算 displayCaption：用**原书图号**（与正文引用一致）+ 图注标题（去掉图注自带图号）。
    原书图号取自 originalNumber 或图注开头的「图X[-Y]」；两者都没有才退回顺序号。"""
    cap = (b.get("caption") or "").strip()
    if not cap:
        b["displayCaption"] = ""
        b["originalNumber"] = None
        return b
    m = FIG_LEAD.match(cap)
    title = cap[m.end():].strip(" 　:：·、,，") if m else cap
    orig = b.get("originalNumber") or (re.sub(r"^图\s*", "", m.group(0)).strip(" 　:：·、,，") if m else None)
    if not orig:
        orig = b.get("number")
    b["displayCaption"] = (("图%s %s" % (orig, title)) if title else "图%s" % orig).strip()
    b["originalNumber"] = None      # 已并入 displayCaption，避免阅读器重复显示「原书 X」
    return b


def build_blocks(scan_data, book, hf, fig_plan=None):
    """组装新 blocks + chapters。返回 (blocks, chapters, stats, report_info)。"""
    fig_plan = fig_plan or {}
    old_figs = [b for b in book["blocks"] if b["type"] == "figure"]
    figs_by_page = {}
    for i, f in enumerate(old_figs):
        figs_by_page.setdefault(f.get("page"), []).append((i, f))
    for p in figs_by_page:
        figs_by_page[p].sort(key=lambda x: (x[1].get("y") or 0, x[0]))

    # 标题位置：canon 标题 → (page, y)
    head_pos = {}
    for v in hf:
        if v.get("page"):
            head_pos.setdefault(v["page"], []).append(
                {"type": "heading", "page": v["page"], "y": v["y"] or 0, "col": 0, "text": v["title"],
                 "canon": v["title"], "kind": v["kind"]})

    blocks = []
    kept_labels = 0
    labels_dropped = 0
    cap_changed = 0
    fig_dropped = 0
    fig_cap = {}
    for p in range(1, 430):
        r = scan_data.get(str(p))
        if not r or p in SKIP_PAGES:
            continue
        elems = []
        for h in head_pos.get(p, []):
            elems.append(h)
        has_fig = bool(figs_by_page.get(p))
        for para in r["paras"]:
            t = (para["text"] or "").strip()
            if not t:
                continue
            if cjk_len(t) <= 2 and not t.startswith(("（", "(")):
                continue
            # 图版页上的「图名清单」：无句读、不以括号开头的长行，不是正文（并入图注归档）
            if has_fig and cjk_len(t) >= 8 and not t.startswith(("（", "(")) \
                    and not any(ch in "。，、；：！？…—" for ch in t):
                labels_dropped += 1
                continue
            elems.append({"type": "paragraph", "page": p, "y": para["y"], "col": 0,
                          "indent": 1, "text": t})
        kept_labels += len(r.get("labels") or [])

        # 图注分配
        figs = figs_by_page.get(p, [])
        caps = [c for c in (r.get("captions") or []) if clean_caption(c["text"])]
        caps_figs = {}
        if figs:
            if len(caps) == len(figs):
                for (_, f), c in zip(figs, sorted(caps, key=lambda x: x["y"])):
                    caps_figs[f["id"]] = c["text"]
            else:
                # 先按同图号替换（修被截断的图注）
                for _, f in figs:
                    oldn = cap_number(f.get("caption") or "")
                    if oldn:
                        for c in caps:
                            if cap_number(c["text"]) == oldn:
                                caps_figs[f["id"]] = c["text"]
                                break
                # 再按 y 就近给没有图注的图补
                used = set(caps_figs)
                for _, f in figs:
                    if f["id"] in used or (f.get("caption") or "").strip():
                        continue
                    cand = [c for c in caps if c["text"] not in used
                            and c["y"] >= (f.get("y") or 0) - 20]
                    if cand:
                        c = min(cand, key=lambda c: abs(c["y"] - (f.get("y") or 0)))
                        caps_figs[f["id"]] = c["text"]
                        used.add(c["text"])
        for _, f in figs:
            nf = {k: v for k, v in f.items() if k != "caption"}
            oc = caps_figs.get(f["id"])
            nf["caption"] = oc if oc else (f.get("caption") or "")
            if oc and oc != (f.get("caption") or ""):
                cap_changed += 1
            # 图注方案（figure-audit）优先：fix 用核定文本，delete 的碎片/假图清空图注但保留图块
            pl = fig_plan.get(f["id"])
            if pl and pl["action"] == "delete":
                fig_dropped += 1
                continue      # 图版碎片/重复图块（逐块目视核实）直接不进书稿
            nc = ((pl or {}).get("new_caption") or "").strip()
            old_c = (nf.get("caption") or "").strip()
            if pl and pl["action"] == "fix" and nc.startswith("图") and \
                    len(nc) >= len(old_c) - 3 and nc != old_c:
                nf["caption"] = nc
            nf["caption"] = fix_caption(nf)
            nf = fix_display(nf)
            elems.append(nf)
            fig_cap[f["id"]] = nf["caption"]

        elems.sort(key=lambda e: (e.get("y") or 0,
                                  0 if e["type"] == "heading" else (1 if e["type"] == "paragraph" else 2)))
        blocks += elems

    fixed = apply_gap_repairs(blocks)
    blocks, dropped_paras = drop_label_paras(blocks)
    blocks, merged_cont = merge_continuations(blocks)

    # 章：说明 + 绪论 + 第一~七章 + 附录（按块顺序切；章标题块由 match_headings 定位）
    chapters = []

    def add_chapter(cid, title, start, end, start_page):
        bl = blocks[start:end]
        chapters.append({
            "id": cid, "title": title, "startBlock": start, "startPage": start_page,
            "chars": sum(len(btxt2(b)) for b in bl if b["type"] in ("paragraph", "heading", "table")),
            "figures": sum(1 for b in bl if b["type"] == "figure"),
            "blocks": bl,
        })

    chap_starts = []
    for i, b in enumerate(blocks):
        if b["type"] != "heading":
            continue
        if b.get("kind") == "chapter":
            chap_starts.append((i, b["text"]))
        elif b.get("kind") == "appendix" and (b["text"] or "").startswith("附录一"):
            chap_starts.append((i, "附录"))
    if not chap_starts or chap_starts[0][0] > 0:
        chap_starts.insert(0, (0, "说明"))
    for n, (idx, ctitle) in enumerate(chap_starts):
        end = chap_starts[n + 1][0] if n + 1 < len(chap_starts) else len(blocks)
        add_chapter("ch-%d" % n, ctitle, idx, end, blocks[idx]["page"] if blocks else 1)
    stats = {
        "pages": book["stats"].get("pages", 429),
        "chars": sum(len(btxt2(b)) for b in blocks if b["type"] in ("paragraph", "heading", "table")),
        "paragraphs": sum(1 for b in blocks if b["type"] == "paragraph"),
        "figures": sum(1 for b in blocks if b["type"] == "figure"),
        "paired": sum(1 for b in blocks if b["type"] == "figure" and (b.get("caption") or "").strip()),
        "chapters": len(chapters),
    }
    info = {"gapFixed": fixed, "mergedCont": merged_cont, "droppedParas": dropped_paras,
            "labelsKept": kept_labels, "labelsDropped": labels_dropped,
            "captionChanged": cap_changed, "figDropped": fig_dropped,
            "chapters": [(c["id"], c["title"], c["startBlock"], c["startPage"], c["chars"]) for c in chapters]}
    return blocks, chapters, stats, info


# 缝隙重 OCR / 目视补回的正文（锚 = 前一段结尾文字）。只补 OCR 检测器整行漏掉、页面上确实有的字。
GAP_REPAIRS = [
    {"page": 101, "tail": "大致可分为三个类型", "add": "："},
    {"page": 129, "tail": "4.市场", "add": "："},
    {"page": 239, "tail": "但基本风格仍是辽代的", "add": "（图129）[255]。"},
    {"page": 422, "tail": "作九十丈，",
     "add": "《水经注》卷16（四部丛刊本）及《魏书》卷114“释老志”（百纳本二十四史）均作四十余丈。"},
]


DROP_PARAS = [
    (114, "殿頂用旗尾"),
    (124, "延幕访典道坊"),
    (173, "前面建築屋管平直"),
    (173, "後面建築屋誉起"),
    (101, "山西太原市天龍山石窟然石富寺"),
]


def drop_label_paras(blocks):
    out = []
    n = 0
    for b in blocks:
        if b["type"] == "paragraph":
            t = (b.get("text") or "").lstrip()
            hit = False
            for pg, pre in DROP_PARAS:
                if b.get("page") == pg and t.startswith(pre):
                    hit = True
                    break
            if hit:
                n += 1
                continue
        out.append(b)
    return out, n


def apply_gap_repairs(blocks):
    n = 0
    for rp in GAP_REPAIRS:
        for b in blocks:
            if b["type"] != "paragraph" or b.get("page") != rp["page"]:
                continue
            t = (b.get("text") or "").rstrip()
            if t.endswith(rp["tail"]):
                b["text"] = t + rp["add"]
                n += 1
                break
    return n


# 引文续行白名单：原书同一段引文被 OCR/版式切成两段，逐条目视确认后合并（page, 前段结尾, 后段开头）
MERGE_CONT = [
    (47, "临淄甚富而实，", "其民无不"),
    (54, "周驰为阁", "道，自殿下"),
    (88, "[86]、", "[132]，广州"),
]


def merge_continuations(blocks):
    out = []
    merged = 0
    for b in blocks:
        ok = False
        if b["type"] == "paragraph" and out and out[-1]["type"] == "paragraph":
            prev = out[-1]
            pt = (prev.get("text") or "").rstrip()
            ct = (b.get("text") or "").lstrip()
            for pg, tail, head in MERGE_CONT:
                if prev.get("page") == pg and pt.endswith(tail) and ct.startswith(head):
                    prev["text"] = pt + ct
                    merged += 1
                    ok = True
                    break
        if ok:
            continue
        out.append(b)
    return out, merged


def btxt2(b):
    return b.get("text") or b.get("caption") or ""


def apply(dry=False):
    scan_data = json.load(io.open(SCAN, encoding="utf-8"))
    BEFORE = os.path.join(REVIEW, "book_before.json")
    book = C.load_book()
    oldbook = json.load(io.open(BEFORE, encoding="utf-8")) if os.path.exists(BEFORE) else book
    hf = match_headings(scan_data)
    fig_plan = {}
    if os.path.exists(FIGPLAN):
        fpj = json.load(io.open(FIGPLAN, encoding="utf-8"))
        fig_plan = {x["fig_id"]: x for x in fpj.get("plan", [])}
    blocks, chapters, stats, info = build_blocks(scan_data, book, hf, fig_plan)
    print("新 blocks %d | 段 %d | 图 %d | 章 %d | 标题块 %d" % (
        len(blocks), stats["paragraphs"], stats["figures"], stats["chapters"],
        sum(1 for b in blocks if b["type"] == "heading")))
    print("缝隙补字 %d 处；引文续行合并 %d 处；丢图名垃圾段 %d 个；" % (
        info.get("gapFixed", 0), info.get("mergedCont", 0), info.get("droppedParas", 0)))
    print("图注按图注审计改 %d 条；剔出图版碎片块 %d 个；剔出图内标注行 %d 条；剔出图版图名行 %d 条" % (
        info["captionChanged"], info.get("figDropped", 0), info["labelsKept"],
        info.get("labelsDropped", 0)))
    for c in info["chapters"]:
        print("  %-5s %-24s start=%-5d p%-4d %d 字" % (c[0], c[1][:24], c[2], c[3], c[4]))
    # 守恒
    old = Counter()
    for b in oldbook["blocks"]:
        if b["type"] in ("paragraph", "heading"):
            old.update(C.norm(btxt2(b)))
    new = Counter()
    for b in blocks:
        if b["type"] in ("paragraph", "heading"):
            new.update(C.norm(btxt2(b)))
    miss = old - new
    print("旧正文汉字 %d → 新 %d；旧有新无 %d 字（%.2f%%，图内标注剔除属预期）" % (
        sum(old.values()), sum(new.values()), sum(miss.values()),
        100.0 * sum(miss.values()) / max(1, sum(old.values()))))
    if dry:
        json.dump({"blocks": blocks, "chapters": [dict(c, blocks=None) for c in chapters], "stats": stats},
                  io.open(os.path.join(REVIEW, "_new_blocks.json"), "w", encoding="utf-8"),
                  ensure_ascii=False)
        print("--dry：不写回书稿，新 blocks 已存 _工作记录\\古建史与全库审查\\_new_blocks.json")
        return
    fp = os.path.join(BOOK, "book.json")
    bak = os.path.join(BOOK, "book.json.bak_%s_正文重建前" % time.strftime("%Y%m%d_%H%M"))
    shutil.copy2(fp, bak)
    nb = dict(book)
    nb["blocks"] = blocks
    nb["chapters"] = chapters
    nb["stats"] = stats
    nb["_fix"] = dict(book.get("_fix") or {})
    nb["_fix"]["rebuild"] = {"date": time.strftime("%Y-%m-%d %H:%M"),
                             "tool": "tools/_rebuild_fjz.py"}
    # 图内标注存档（可追溯）
    labels_archive = {p: r.get("labels") for p, r in scan_data.items() if r.get("labels")}
    json.dump(labels_archive, io.open(os.path.join(REVIEW, "_labels_archive.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    tmp = fp + ".tmp"
    json.dump(nb, io.open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, fp)
    print("已写回 %s（备份 %s）" % (fp, os.path.basename(bak)))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    if cmd == "scan":
        lo = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        hi = int(sys.argv[3]) if len(sys.argv) > 3 else 429
        scan(lo, hi)
    elif cmd == "match":
        scan_data = json.load(io.open(SCAN, encoding="utf-8"))
        hf = match_headings(scan_data)
        json.dump(hf, io.open(os.path.join(REVIEW, "heading_match.json"), "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        for v in hf:
            print("p%-4s %s%s" % (v["page"], "" if v["page"] else "!! ", v["title"]))
    elif cmd == "apply":
        apply(dry="--dry" in sys.argv)
    else:
        report()


if __name__ == "__main__":
    main()