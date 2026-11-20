#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
筑学 · PDF → 书稿包（样章版 v2）

把扫描版 PDF 的指定页范围转成「统一排版」所需的结构化数据：
    正文段落（带页码）/ 章节标题 / 插图（裁切 + 白底归一化）/ 图注（配对）/ 图号重排映射

用法：
    python pdf2book.py <pdf> <起始页> <结束页> <输出目录> [--title 书名]
"""
import os
import re
import sys
import json
import time
import argparse
from collections import Counter

import numpy as np
from PIL import Image, ImageFilter
from rapidocr_onnxruntime import RapidOCR
import pypdfium2 as pdfium

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from book_preview import build_preview as build_book_preview

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

SCALE = 3.0            # OCR 渲染倍率（≈225dpi，比 2 倍更少漏字）
HIRES_SCALE = 4.0      # 裁图用的高清倍率（≈300dpi）
CELL = 16
FIG_MIN_AREA = 22000
FIG_MIN_SIDE = 90
HEADER_RATIO = 0.100
FOOTER_RATIO = 0.860

CAPTION_RE = re.compile(r"^图\s?([0-9０-９]+)\s?[-—~－.．·一]\s?([0-9０-９]+)")
HEAD_RE = re.compile(r"^(第\s?[0-9一二三四五六七八九十百]+\s?[章篇节]|\d+(?:\.\d+){1,2}(?=\s|[\u4e00-\u9fa5]))")
TAIL_NUM_RE = re.compile(r"^\s*(\d{1,4})\s*$")
WATERMARK_RE = re.compile(r"(大康学长|超级笔记|超级等记|Bilibili|bilibili|KKKarchi|KKK|微信)")


# ---------------------------------------------------------------- 渲染
def _png_ok(p):
    """校验缓存 PNG 是否完整（进程被强杀会留下半截文件）。"""
    try:
        from PIL import Image as _Img
        with _Img.open(p) as im:
            im.verify()
        return os.path.getsize(p) > 1024
    except Exception:
        return False


def render_page(pdf, n, scale, cache_dir):
    p = os.path.join(cache_dir, f"page_{n:03d}_x{int(scale)}.png")
    if os.path.exists(p) and not _png_ok(p):
        try:
            os.remove(p)
        except OSError:
            pass
    if not os.path.exists(p):
        img = pdf[n - 1].render(scale=scale).to_pil().convert("RGB")
        tmp = p + ".tmp"
        img.save(tmp, format="PNG")
        os.replace(tmp, p)   # 原子替换：即使中途被杀也不会留下半截 PNG
    return p


# ---------------------------------------------------------------- 文字层直读（电子版 PDF）
def _pymupdf():
    import pymupdf
    return pymupdf


def has_text_layer(pdf_path, sample=8, min_chars=120):
    """抽样检查 PDF 是否带「可用」文字层；电子版书籍可直接读文字，免 OCR。

    注意：有的扫描件带一层乱码文字（如 !!"#$%&），字符数够但内容不可读，
    这种必须走 OCR。所以除了字符数，还要看中文占比。
    """
    try:
        pymupdf = _pymupdf()
    except Exception:
        return False
    try:
        doc = pymupdf.open(pdf_path)
        n = min(sample, doc.page_count)
        if n == 0:
            return False
        total, cjk, alpha = 0, 0, 0
        for i in range(n):
            t = doc.load_page(i).get_text("text").strip()
            total += len(t)
            cjk += sum(1 for ch in t if "\u4e00" <= ch <= "\u9fff")
            alpha += sum(1 for ch in t if ch.isalpha() and ch.isascii())
        doc.close()
        if total / n < min_chars:
            return False
        # 中文占比过低（乱码文字层，字符数够但读不出内容）→ 判为无文字层，走 OCR
        if total and cjk / total < 0.05:
            return False
        return True
    except Exception:
        return False


def text_layer_lines(pdf_path, n, scale, cache_json):
    """把 PDF 文字层转成与 OCR 相同的行结构（坐标按 scale 放大到渲染图坐标系）。"""
    if os.path.exists(cache_json):
        with open(cache_json, encoding="utf-8") as f:
            return json.load(f)
    pymupdf = _pymupdf()
    doc = pymupdf.open(pdf_path)
    page = doc.load_page(n - 1)
    data = page.get_text("dict")
    lines = []
    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = "".join(sp.get("text", "") for sp in line.get("spans", []))
            if not text.strip():
                continue
            x0, y0, x1, y1 = line.get("bbox", (0, 0, 0, 0))
            lines.append({
                "text": text.strip(),
                "x0": float(x0) * scale, "y0": float(y0) * scale,
                "x1": float(x1) * scale, "y1": float(y1) * scale,
                "conf": 1.0,
            })
    doc.close()
    lines.sort(key=lambda l: (round(l["y0"]), l["x0"]))
    with open(cache_json, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False)
    return lines


def detect_gutter(img):
    """双栏页返回栏缝 x 坐标；单栏返回 None（按页面竖向墨迹投影判断）。"""
    g = img.convert("L")
    W, H = g.size
    arr = np.asarray(g, dtype=np.uint8)
    band = arr[int(H * 0.12):int(H * 0.88), :]
    ink = (band < 200).astype(np.float32)
    colsum = ink.sum(axis=0)
    lo, hi = int(W * 0.42), int(W * 0.58)
    if hi - lo < 30:
        return None
    win = max(8, W // 100)
    best_x, best_v = None, 1e18
    for x in range(lo, hi - win):
        v = float(colsum[x:x + win].mean())
        if v < best_v:
            best_v, best_x = v, x + win // 2
    if best_x is None:
        return None
    avg = float(colsum.mean()) + 1e-6
    left_ink = float(ink[:, :best_x].sum())
    right_ink = float(ink[:, best_x:].sum())
    total = float(ink.size) + 1e-6
    if best_v < avg * 0.35 and left_ink / total > 0.01 and right_ink / total > 0.01:
        return best_x
    return None


def ocr_page_lines(engine, page_path, cache_json):
    """双栏页按栏裁剪分别 OCR，避免左右栏被识别成同一行。"""
    img = Image.open(page_path).convert("RGB")
    if os.path.exists(cache_json):
        with open(cache_json, encoding="utf-8") as f:
            raw = json.load(f)
        return merge_column_fragments(raw, detect_gutter(img))
    # 兼容旧缓存（早期版本只有 page_*_ocr.json），避免恢复时重新识别
    legacy = cache_json.replace("_ocr2.json", "_ocr.json")
    if os.path.exists(legacy):
        with open(legacy, encoding="utf-8") as f:
            raw = json.load(f)
        return merge_column_fragments(raw, detect_gutter(img))
    W, H = img.size
    gutter = detect_gutter(img)
    if gutter and 0.2 * W < gutter < 0.8 * W:
        crops = [(img.crop((0, 0, min(W, gutter + 20), H)), 0),
                 (img.crop((max(0, gutter - 20), 0, W, H)), max(0, gutter - 20))]
    else:
        crops = [(img, 0)]
    lines = []
    for crop, ox in crops:
        res, _ = engine(np.asarray(crop))
        for box, text, conf in res or []:
            text = (text or "").strip()
            if not text:
                continue
            xs = [float(q[0]) for q in box]
            ys = [float(q[1]) for q in box]
            lines.append({
                "text": text,
                "x0": min(xs) + ox, "y0": min(ys), "x1": max(xs) + ox, "y1": max(ys),
                "conf": float(conf),
            })
    lines.sort(key=lambda l: (round(l["y0"] / 6), l["x0"]))
    with open(cache_json, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False)
    return merge_column_fragments(lines, gutter)


def merge_column_fragments(lines, gutter):
    """把被栏缝切开的整行标题（左右两半）接回去。"""
    if not gutter:
        return lines
    merged = []
    for l in sorted(lines, key=lambda x: (round(x["y0"] / 6), x["x0"])):
        attached = False
        for k in range(len(merged) - 1, max(-1, len(merged) - 4), -1):
            prev = merged[k]
            h = max(1.0, prev["y1"] - prev["y0"])
            oy = min(prev["y1"], l["y1"]) - max(prev["y0"], l["y0"])
            if (oy > 0.3 * h and prev["x1"] >= gutter - 12
                    and gutter - 30 <= l["x0"] <= gutter + 15):
                prev["text"] += l["text"]
                prev["x1"] = max(prev["x1"], l["x1"])
                prev["y1"] = max(prev["y1"], l["y1"])
                prev["y0"] = min(prev["y0"], l["y0"])
                attached = True
                break
        if not attached:
            merged.append(dict(l))
    return merged


def load_ocr(engine, page_path, cache_json):
    if os.path.exists(cache_json):
        with open(cache_json, encoding="utf-8") as f:
            return json.load(f)
    res, _ = engine(page_path)
    lines = []
    for box, text, conf in res or []:
        xs = [float(q[0]) for q in box]
        ys = [float(q[1]) for q in box]
        lines.append({
            "text": text.strip(),
            "x0": min(xs), "y0": min(ys), "x1": max(xs), "y1": max(ys),
            "conf": float(conf),
        })
    with open(cache_json, "w", encoding="utf-8") as f:
        json.dump(lines, f, ensure_ascii=False)
    return lines


# ---------------------------------------------------------------- 图像处理
def normalize_paper(img):
    """白底归一化：在缩小图上估计背景，再放大回去做除法；比全分辨率 MaxFilter 快几十倍。"""
    im = img.convert("RGB")
    w, h = im.size
    f = max(1, int(round(min(w, h) / 240.0)))
    small = im.resize((max(1, w // f), max(1, h // f)), Image.BILINEAR)
    sarr = np.asarray(small, dtype=np.float32)
    k = max(3, int(round(25 / f)) | 1)
    bg_small = np.stack([
        np.asarray(Image.fromarray(sarr[:, :, c].astype(np.uint8)).filter(ImageFilter.MaxFilter(k)),
                   dtype=np.float32)
        for c in range(3)
    ], axis=2)
    bg = np.asarray(
        Image.fromarray(np.clip(bg_small, 1, 255).astype(np.uint8)).resize((w, h), Image.BILINEAR),
        dtype=np.float32)
    arr = np.asarray(im, dtype=np.float32)
    out = arr / np.clip(bg, 1, 255) * 255.0
    lo, hi = np.percentile(out, 0.5), np.percentile(out, 99.7)
    out = (out - lo) / max(1e-6, hi - lo) * 255.0
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def figure_regions(img, text_boxes, lh):
    """插图区域检测：
    - 方差法抓照片/网点图
    - 墨迹法抓工程线图（平面、剖面、立面这类细线图方差低，必须用墨迹连通性）
    两者取并集，再按“非文字区域”做连通域合并。
    """
    W, H = img.size
    gw, gh = (W + CELL - 1) // CELL, (H + CELL - 1) // CELL
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    g = np.pad(gray, ((0, max(0, gh * CELL - H)), (0, max(0, gw * CELL - W))), mode="edge")
    g = g[: gh * CELL, : gw * CELL]
    cells = g.reshape(gh, CELL, gw, CELL).transpose(0, 2, 1, 3).reshape(gh, gw, CELL * CELL)
    std = cells.std(axis=2)
    ink = (cells < 232).sum(axis=2) >= 6          # 有足够墨迹的格子（细线图也能抓到）
    cand = (std > 24) | ink
    # 排除页眉页脚带
    cand[: int(gh * HEADER_RATIO)] = False
    cand[int(gh * 0.90):] = False
    for x0, y0, x1, y1 in text_boxes:
        cand[max(0, int(y0 // CELL) - 1): int(y1 // CELL) + 2,
             max(0, int(x0 // CELL) - 1): int(x1 // CELL) + 2] = False

    # 连通域（4 邻域）
    seen = np.zeros_like(cand)
    regions = []
    for r in range(gh):
        for c in range(gw):
            if not cand[r, c] or seen[r, c]:
                continue
            stack = [(r, c)]
            seen[r, c] = True
            cells = []
            while stack:
                y, x = stack.pop()
                cells.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and cand[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(cells) < 36:
                continue
            rs = [y for y, _ in cells]
            cs = [x for _, x in cells]
            x0, x1 = min(cs) * CELL, min(W, (max(cs) + 1) * CELL)
            y0, y1 = min(rs) * CELL, min(H, (max(rs) + 1) * CELL)
            if x1 - x0 >= 50 and y1 - y0 >= 50 and (x1 - x0) * (y1 - y0) >= 9000:
                regions.append([x0, y0, x1, y1])

    # 保留原始联通块（不做邻近合并，交由“图注锚点”归组）
    merged = [r for r in regions]
    merged.sort(key=lambda b: (round(b[1] / max(1, lh * 2)), b[0]))
    return merged


def order_figures(figs):
    """同一页内按原书图号排序（更符合书籍编号逻辑），无编号的排后面。"""
    def key(f):
        m = CAPTION_RE.match(f.get("caption") or "")
        if m:
            return (0, int(m.group(1)) * 1000 + int(m.group(2)))
        return (1, f.get("y", 0))
    return sorted(figs, key=key)


# ---------------------------------------------------------------- 版面解析
def parse_page(pdf, n, ocr_lines, cache_dir, hi_dir, figs_dir, fig_seq, lh_default=40.0, header_set=None, footer_set=None):
    pocr = os.path.join(cache_dir, f"page_{n:03d}_x{int(SCALE)}.png")
    img = Image.open(pocr).convert("RGB")
    W, H = img.size

    lines = [dict(l) for l in ocr_lines if l["text"] and not WATERMARK_RE.search(l["text"])]
    # 页眉/页脚
    body = []
    printed_page = None
    for ln in lines:
        txt = ln["text"].strip()
        # 页眉/页脚只删「跨页重复出现」的行；紧贴页边的正文第一行不再一刀切删掉
        if header_set is not None:
            if txt in header_set and ln["y1"] < H * 0.22:
                continue
        elif ln["y1"] < H * HEADER_RATIO:
            continue
        if ln["y0"] > H * FOOTER_RATIO and TAIL_NUM_RE.match(txt):
            printed_page = int(txt)
            continue
        if footer_set is not None and txt in footer_set and ln["y0"] > H * 0.78:
            continue
        body.append(ln)

    # ---- 双栏页面：跨两栏的合并行按栏缝拆开（OCR 常把左右栏同一行识别成一行）----
    gutter = None
    right_starts = sorted(l["x0"] for l in body if l["x0"] > W * 0.45)
    if len(right_starts) >= 8 and len(right_starts) >= len(body) * 0.2:
        gutter = right_starts[len(right_starts) // 2] - 10
    if gutter:
        split_body = []
        for l in body:
            if (l["x0"] < gutter - 30 and l["x1"] > gutter + 30
                    and (l["x1"] - l["x0"]) > W * 0.25):
                t = l["text"]
                ratio = (gutter - l["x0"]) / max(1, l["x1"] - l["x0"])
                idx = max(1, min(len(t) - 1, int(round(len(t) * ratio))))
                left = dict(l); left["text"] = t[:idx].strip(); left["x1"] = gutter - 2
                right = dict(l); right["text"] = t[idx:].strip(); right["x0"] = gutter + 2
                if left["text"]:
                    split_body.append(left)
                if right["text"]:
                    split_body.append(right)
            else:
                split_body.append(l)
        body = split_body

    heights = sorted(l["y1"] - l["y0"] for l in body)
    lh = heights[len(heights) // 2] if heights else lh_default

    # ---- 图注识别（支持同一行左右两个图注、支持续行） ----
    body.sort(key=lambda l: (round(l["y0"] / 6), l["x0"]))
    used = set()
    caps = []
    for i, ln in enumerate(body):
        if i in used:
            continue
        m = CAPTION_RE.match(ln["text"])
        if not m:
            continue
        text = ln["text"]
        used.add(i)
        # 续行：必须与图注水平重叠、垂直紧邻、且较短；中间夹着别的图注时跳过它继续找
        j = i + 1
        while j < len(body) and j < i + 4 and j not in used:
            nxt = body[j]
            if CAPTION_RE.match(nxt["text"]):
                j += 1
                continue
            vgap = nxt["y0"] - ln["y1"]
            cap_w = ln["x1"] - ln["x0"]
            nxt_w = nxt["x1"] - nxt["x0"]
            starts_near = abs(nxt["x0"] - ln["x0"]) < lh * 1.2
            prefix_ok = nxt["text"].strip().startswith(("上：", "下：", "左：", "右：", "中：", "上:", "下:", "左:", "右:", "中:"))
            truncated_paren = bool(re.search(r"[（(][^）)]*$", nxt["text"]))
            if (vgap < lh * 1.6 and (starts_near or prefix_ok)
                    and (nxt_w <= max(cap_w * 1.6, lh * 5) or prefix_ok)
                    and len(nxt["text"]) <= 40 and not truncated_paren
                    and len(text) + len(nxt["text"]) <= 60
                    and (prefix_ok or len(nxt["text"]) <= 18)
                    and not re.search(r"[。！？；]$", nxt["text"])):
                if text[-1] not in "。！？，、；":
                    text += nxt["text"]
                    used.add(j)
                    j += 1
                    continue
            break
        caps.append({"text": text, "box": [ln["x0"], ln["y0"], ln["x1"], ln["y1"]], "orig": m.group(0)})

    body = [l for i, l in enumerate(body) if i not in used]

    # ---- 插图区域（墨迹/方差联通块）----
    regions = figure_regions(img, [[l["x0"], l["y0"], l["x1"], l["y1"]] for l in body] + [c["box"] for c in caps], lh)

    def union(regs):
        return [min(r[0] for r in regs), min(r[1] for r in regs),
                max(r[2] for r in regs), max(r[3] for r in regs)]

    # 图注锚点归组：每个联通块归给最近的图注，同一图注下的多个子块合成一幅图。
    # 这样“四根柱头 = 一幅图”与“并排三幅图”都能正确区分。
    boxes = []
    if caps:
        groups = {i: [] for i in range(len(caps))}
        orphans = []
        for reg in regions:
            cx, cy = (reg[0] + reg[2]) / 2, (reg[1] + reg[3]) / 2
            best, bestd = None, 1e18
            for i, c in enumerate(caps):
                ccx = (c["box"][0] + c["box"][2]) / 2
                ccy = (c["box"][1] + c["box"][3]) / 2
                dx = max(0, abs(cx - ccx) - (reg[2] - reg[0]) / 2 - (c["box"][2] - c["box"][0]) / 2)
                dy = max(0, cy - ccy)   # 图注通常在图的下面
                d = (dx * dx + dy * dy * 0.5) ** 0.5
                if d < bestd:
                    best, bestd = i, d
            if best is not None and bestd < H * 0.45:
                groups[best].append(reg)
            else:
                orphans.append(reg)
        for i, regs in groups.items():
            if regs:
                box = union(regs)
                if (box[2] - box[0]) >= FIG_MIN_SIDE and (box[3] - box[1]) >= FIG_MIN_SIDE \
                        and (box[2] - box[0]) * (box[3] - box[1]) >= FIG_MIN_AREA:
                    boxes.append((box, caps[i]))
        for r in orphans:
            if (r[2] - r[0]) * (r[3] - r[1]) > FIG_MIN_AREA * 2:
                boxes.append((r, None))
    else:
        boxes = [(r, None) for r in regions]


    # 高清页（用于裁图）
    hi_path = os.path.join(cache_dir, f"page_{n:03d}_x4.png")
    hi_img = Image.open(hi_path).convert("RGB") if os.path.exists(hi_path) else None
    k = (hi_img.size[0] / W) if hi_img else 1.0

    figs = []
    used_caps = set()
    for box, cap in boxes:
        x0, y0, x1, y1 = box
        # 图注不能被裁进图片里：把下边界收在图注上方
        cap_tops = [c["box"][1] for c in caps if c["box"][1] >= y1 - lh * 1.2]
        if cap_tops:
            y1 = min(y1, min(cap_tops) - 3)
        pad = 10
        y_bottom = min(H, y1 + pad)
        if cap_tops:
            y_bottom = min(y_bottom, min(cap_tops) - 3)
        crop_box = (max(0, x0 - pad), max(0, y0 - pad), min(W, x1 + pad), y_bottom)
        if hi_img:
            hbox = tuple(int(round(v * k)) for v in crop_box)
            crop = hi_img.crop(hbox)
        else:
            crop = img.crop(crop_box)
        clean = normalize_paper(crop)
        fig_seq["n"] += 1
        fid = f"fig-{fig_seq['n']:03d}"
        clean.save(os.path.join(figs_dir, fid + ".webp"), quality=92)
        figs.append({"id": fid, "src": f"assets/{fid}.webp", "box": list(crop_box), "y": crop_box[1],
                     "caption": cap["text"] if cap else None,
                     "orig": cap["orig"] if cap else None})
        if cap:
            used_caps.add(id(cap))

    # 没配到插图的图注：尝试把「图注上方、上一行正文之下」的区域裁成图（补图）
    for cap in caps:
        if id(cap) in used_caps:
            continue
        cx0, cy0, cx1, cy1 = cap["box"]
        above = [l["y1"] for l in body if l["y1"] < cy0 - lh * 0.3 and l["y1"] > H * HEADER_RATIO]
        band_top = int(max(above) + lh * 0.5) if above else int(H * HEADER_RATIO)
        band_bottom = int(cy0 - 4)
        if band_bottom - band_top < 60:
            continue
        x_from, x_to = int(W * 0.05), int(W * 0.95)
        band = np.asarray(img.convert("L").crop((x_from, band_top, x_to, band_bottom)), dtype=np.float32)
        ink = band < 200
        rows = np.where(ink.any(axis=1))[0]
        cols = np.where(ink.any(axis=0))[0]
        if len(rows) < 20 or len(cols) < 30:
            continue
        x0 = x_from + int(cols.min()) - 6
        x1 = x_from + int(cols.max()) + 6
        y0 = band_top + int(rows.min()) - 6
        y1 = band_top + int(rows.max()) + 6
        if (x1 - x0) < 60 or (y1 - y0) < 60 or (x1 - x0) * (y1 - y0) < 12000:
            continue
        crop_box = (max(0, x0), max(0, y0), min(W, x1), min(H, y1))
        if hi_img:
            hbox = tuple(int(round(v * k)) for v in crop_box)
            crop = hi_img.crop(hbox)
        else:
            crop = img.crop(crop_box)
        clean = normalize_paper(crop)
        fig_seq["n"] += 1
        fid = f"fig-{fig_seq['n']:03d}"
        clean.save(os.path.join(figs_dir, fid + ".webp"), quality=92)
        figs.append({"id": fid, "src": f"assets/{fid}.webp", "box": list(crop_box), "y": crop_box[1],
                     "caption": cap["text"], "orig": cap["orig"]})
        used_caps.add(id(cap))

    # 仍然没有图的图注：作为普通文字保留，避免丢内容
    for cap in caps:
        if id(cap) not in used_caps:
            body.append({"text": cap["text"], "x0": cap["box"][0], "y0": cap["box"][1],
                         "x1": cap["box"][2], "y1": cap["box"][3], "conf": 1.0, "_caption": True})

    # 图内标注（短、且大部分落在图框里）从正文剔除；长行/正文不删
    def inside_fig(l):
        lw = max(1.0, l["x1"] - l["x0"])
        cx = (l["x0"] + l["x1"]) / 2
        cy = (l["y0"] + l["y1"]) / 2
        for f in figs:
            bx0, by0, bx1, by1 = f["box"]
            if not (bx0 <= cx <= bx1 and by0 <= cy <= by1):
                continue
            overlap = max(0.0, min(l["x1"], bx1) - max(l["x0"], bx0))
            t = l["text"].strip()
            fully_inside = l["x0"] >= bx0 - 6 and l["x1"] <= bx1 + 6
            if fully_inside and len(t) <= 14 and not re.search(r"[，。！？；：、]", t):
                return True
        return False
    body = [l for l in body if not inside_fig(l)]

    # B) 图注续行（“上：/下：/左：/右：/中：”或标准图号“图7-4…”）并入对应插图
    cap_prefix = ("上：", "下：", "左：", "右：", "中：", "上:", "下:", "左:", "右:", "中:")
    cap_num_re = re.compile(r"^图\s?\d+\s?[-—~－.．·]\s?\d+")
    for ln in list(body):
        t = ln["text"].strip()
        is_num_cap = bool(cap_num_re.match(t)) and len(t) <= 32 and "）" not in t[:8] and ")" not in t[:8]
        if not (t.startswith(cap_prefix) or is_num_cap) or len(t) > 42:
            continue
        best, bestd = None, 1e18
        for f in figs:
            bx0, by0, bx1, by1 = f["box"]
            dx = abs(((ln["x0"] + ln["x1"]) / 2) - ((bx0 + bx1) / 2))
            dy = min(abs(ln["y0"] - by1), abs(ln["y1"] - by0))
            d = dx + dy * 0.5
            if d < bestd:
                best, bestd = f, d
        if best is not None and bestd < H * 0.25:
            best["caption"] = ((best.get("caption") or "") + " " + t).strip()
            body.remove(ln)


    # ---- 段落组装：以「栏的左边距 + 首行缩进」为准；支持双栏 ----
    from collections import Counter as _Counter

    def assemble(lines, col=0):
        lines = sorted(lines, key=lambda l: (round(l["y0"] / 6), l["x0"]))
        margins = _Counter(int(round(l["x0"] / 6.0)) * 6 for l in lines)
        margin = margins.most_common(1)[0][0] if margins else 0
        out = []
        cur = None
        for ln in lines:
            text = ln["text"]
            if ln.get("_caption"):
                if cur:
                    out.append(cur); cur = None
                out.append({"type": "captiontext", "text": text, "y": ln["y0"], "page": n, "col": col})
                continue
            if (HEAD_RE.match(text) and len(text) < 34
                    and not re.search(r"[。！？，；：]$", text)):
                if cur:
                    out.append(cur); cur = None
                out.append({"type": "heading", "text": text, "y": ln["y0"], "page": n, "col": col})
                continue
            starts_punct = bool(re.match(r"^[，。、；：）】」』”’！？]", text))
            indent = (ln["x0"] - margin) > lh * 0.7 and not starts_punct
            gap = ln["y0"] - cur["_y1"] if cur else 0
            prev_term = bool(cur and cur["text"].strip() and cur["text"].strip()[-1] in "。！？…：；”』」）)】.")
            new_para = (cur is None
                        or (indent and (prev_term or gap > lh * 1.6))
                        or gap > lh * 2.2)
            if new_para:
                if cur:
                    out.append(cur)
                cur = {"type": "paragraph", "text": text, "y": ln["y0"], "page": n, "col": col,
                       "indent": bool(indent), "_y1": ln["y1"], "_x0": ln["x0"]}
            else:
                joiner = " " if (re.search(r"[A-Za-z0-9]$", cur["text"]) and re.match(r"^[A-Za-z0-9]", text)) else ""
                cur["text"] += joiner + text
                cur["_y1"] = ln["y1"]
                cur["_x0"] = min(cur["_x0"], ln["x0"])
        if cur:
            out.append(cur)
        for p in out:
            p.pop("_y1", None)
            p.pop("_x0", None)
        return out

    def build_flow(paras, figs_col):
        flow = []
        fig_iter = sorted(figs_col, key=lambda f: f["y"])
        idx = 0
        for p in paras:
            while idx < len(fig_iter) and fig_iter[idx]["y"] <= p.get("y", 1e9):
                f = fig_iter[idx]
                flow.append({"type": "figure", "id": f["id"], "src": f["src"], "page": n,
                             "y": f["y"], "box": list(f["box"]), "caption": f["caption"], "originalCaption": f["orig"]})
                idx += 1
            if p["type"] == "captiontext":
                flow.append({"type": "paragraph", "text": p["text"], "y": p["y"], "page": n})
                continue
            flow.append(p)
        while idx < len(fig_iter):
            f = fig_iter[idx]
            flow.append({"type": "figure", "id": f["id"], "src": f["src"], "page": n,
                         "y": f["y"], "box": list(f["box"]), "caption": f["caption"], "originalCaption": f["orig"]})
            idx += 1
        return flow

    # 双栏检测：右侧独立文字足够多就按栏读（左栏读完再读右栏）
    right_lines = [l for l in body if l["x0"] > W * 0.45]
    left_lines = [l for l in body if l["x0"] <= W * 0.45]
    two_col = (gutter is not None and len(left_lines) >= 8 and len(right_lines) >= 8)

    if two_col:
        paras = assemble(left_lines, 0) + assemble(right_lines, 1)
        left_figs, right_figs, span_figs = [], [], []
        for f in figs:
            w = f["box"][2] - f["box"][0]
            cx = (f["box"][0] + f["box"][2]) / 2
            if w > W * 0.6:
                span_figs.append(f)
            elif cx <= W * 0.45:
                left_figs.append(f)
            else:
                right_figs.append(f)
        left_flow = build_flow([p for p in paras if p.get("col") == 0], left_figs)
        right_flow = build_flow([p for p in paras if p.get("col") == 1], right_figs)
        # 跨栏接段：左栏最后一段没句末标点，右栏第一段又不是缩进新段 → 接上
        last_para = None
        for b in reversed(left_flow):
            if b["type"] == "paragraph":
                last_para = b
                break
        if last_para is not None:
            lt = last_para["text"].strip()
            if lt and lt[-1] not in "。！？…：；”』」）)】":
                for i, b in enumerate(right_flow):
                    if b["type"] == "paragraph":
                        last_para["text"] = lt + b["text"].strip()
                        right_flow.pop(i)
                        break
                    if b["type"] == "heading":
                        break
        flow = left_flow + right_flow + build_flow([], span_figs)
    else:
        paras = assemble(body, 0)
        flow = build_flow(paras, figs)
    return flow, figs, printed_page


# ---------------------------------------------------------------- 后处理
def merge_cross_page(blocks):
    """跨页段落合并：允许中间夹着插图，只要上一段没有句末标点、下一段没有首行缩进。"""
    out = []
    for b in blocks:
        merged = False
        if b["type"] == "paragraph":
            for k in range(len(out) - 1, max(-1, len(out) - 4), -1):
                prev = out[k]
                if prev["type"] != "paragraph":
                    continue
                if prev["page"] != b["page"]:
                    tail = prev["text"][-1] if prev["text"] else ""
                    if tail and tail not in "。！？…：；”』」）)】.．!?":
                        prev["text"] += b["text"]
                        prev["pages"] = sorted({prev["page"], b["page"]})
                        merged = True
                break
        if not merged:
            out.append(b)
    return out


REF_RE = re.compile(r"图\s*([0-9０-９]+)\s*[-—~－.．·一]\s*([0-9０-９]+)")
FW_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")


def norm_orig(num_str):
    """'图1－10' / '图1-10' → '1-10'"""
    m = REF_RE.match(num_str)
    if not m:
        return None
    return f"{m.group(1).translate(FW_DIGITS)}-{m.group(2).translate(FW_DIGITS)}"


def renumber_figures(blocks):
    figs = [b for b in blocks if b["type"] == "figure"]

    def fig_key(b):
        key = norm_orig(b.get("originalCaption") or "")
        if key:
            a, b2 = key.split("-")
            return (b["page"], int(a) * 1000 + int(b2))
        return (b["page"], 900000 + int(b.get("y", 0)))

    figs.sort(key=fig_key)
    mapping = {}
    for i, b in enumerate(figs, start=1):
        b["number"] = i
        full = (b.get("caption") or "").strip()
        m = CAPTION_RE.match(full)
        if m:
            b["originalNumber"] = re.sub(r"^图\s*", "", m.group(0))
            title = full[m.end():].strip(" 　:：·、")
            key = norm_orig(m.group(0))
            if key:
                mapping[key] = f"图{i}"
        else:
            title = full
        b["displayCaption"] = f"图{i}" + ((" " + title) if title else "")

    def repl(m):
        key = f"{m.group(1).translate(FW_DIGITS)}-{m.group(2).translate(FW_DIGITS)}"
        return mapping.get(key, m.group(0))

    for b in blocks:
        if b["type"] == "paragraph":
            b["text"] = REF_RE.sub(repl, b["text"])

    i = 0
    while i < len(blocks):
        if blocks[i]["type"] == "figure":
            j = i
            while j < len(blocks) and blocks[j]["type"] == "figure":
                j += 1
            blocks[i:j] = sorted(blocks[i:j], key=lambda b: b["number"])
            i = j
        else:
            i += 1
    return mapping


# ---------------------------------------------------------------- 章节拆分
CHAP_RE = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?章")


def split_chapters(blocks, fallback_title):
    """按「第N章」标题切分章节；无章标题时整体作为一章。"""
    chapters = []
    cur = {"id": "ch-1", "title": fallback_title, "startBlock": 0, "blocks": []}
    for i, b in enumerate(blocks):
        if b["type"] == "heading" and CHAP_RE.match(b["text"]):
            if cur["blocks"]:
                chapters.append(cur)
            cur = {"id": f"ch-{len(chapters) + 1}", "title": b["text"].strip(), "startBlock": i, "blocks": []}
            continue
        cur["blocks"].append(b)
    if cur["blocks"]:
        chapters.append(cur)
    if not chapters:
        chapters = [{"id": "ch-1", "title": fallback_title, "startBlock": 0, "blocks": blocks}]
    for ch in chapters:
        pages = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(pages) if pages else None
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b["type"] == "figure")
    return chapters


# ---------------------------------------------------------------- 预览（翻页版）
def build_preview(book, out_path):
    blocks_json = json.dumps(book["blocks"], ensure_ascii=False).replace("</", "<\\/")
    css = """
    *{box-sizing:border-box}
    html,body{margin:0;height:100%}
    body{background:#e8e8e8;font-family:"Songti SC","Noto Serif SC","Source Han Serif SC",Georgia,"Microsoft YaHei",serif;display:flex;flex-direction:column;overflow:hidden}
    .topbar{width:100%;display:flex;justify-content:space-between;gap:12px;padding:10px 20px;font-size:12px;color:#666;background:#f4f4f4;border-bottom:1px solid #ddd}
    .topbar b{color:#333;font-weight:600;letter-spacing:.08em}
    .stage{flex:1;display:flex;align-items:center;justify-content:center;width:100%;padding:16px;position:relative;min-height:0}
    .sheet{width:820px;height:1160px;background:#fff;box-shadow:0 12px 44px rgba(0,0,0,.20);padding:72px 80px 92px;position:relative;display:none;color:#111}
    .sheet.is-active{display:block;animation:turn .18s ease-out}
    @keyframes turn{from{opacity:.35;transform:translateX(6px)}to{opacity:1;transform:none}}
    .sheet .running{position:absolute;top:34px;left:80px;right:80px;display:flex;justify-content:space-between;font-size:11px;color:#b0b0b0;letter-spacing:.06em}
    .sheet .content{width:660px;height:996px;overflow:hidden}
    .sheet .pageno{position:absolute;bottom:40px;left:0;right:0;text-align:center;font-size:12px;color:#888;letter-spacing:.12em}
    h1{font-size:30px;margin:0 0 12px;font-weight:600;letter-spacing:.02em}
    h2{font-size:20px;margin:0 0 16px;font-weight:600}
    p{font-size:18px;line-height:2;text-align:justify;margin:0 0 1em;text-indent:2em;color:#111}
    .page-mark{font-size:10px;color:#c4c4c4;margin-left:4px;vertical-align:super;text-indent:0;letter-spacing:0}
    figure{margin:20px 0 24px;text-align:center}
    figure img{max-width:100%;max-height:620px;object-fit:contain;background:#fff}
    figcaption{font-size:12.5px;color:#555;margin-top:9px;line-height:1.7}
    figcaption .orig{color:#a0a0a0;margin-left:8px;font-size:11px}
    .titlepage{height:100%;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center}
    .titlepage .bk{font-size:13px;letter-spacing:.34em;color:#999}
    .titlepage h1{font-size:34px;line-height:1.5;margin:22px 0 26px;max-width:560px}
    .titlepage .meta{font-size:12px;color:#999;line-height:2.1}
    .nav{position:absolute;top:50%;transform:translateY(-50%);width:44px;height:44px;border-radius:50%;border:1px solid #ccc;background:#fff;color:#444;cursor:pointer;display:grid;place-items:center;box-shadow:0 2px 10px rgba(0,0,0,.08);z-index:5}
    .nav:hover{background:#f7f7f7}
    .nav.prev{left:22px}
    .nav.next{right:22px}
    .nav:disabled{opacity:.25;cursor:default}
    .bottom{width:100%;padding:10px 0 14px;display:flex;flex-direction:column;align-items:center;gap:7px;background:#f4f4f4;border-top:1px solid #ddd}
    .counter{font-size:12px;color:#666;letter-spacing:.06em}
    .bar{width:280px;height:4px;background:#dcdcdc;border-radius:2px;overflow:hidden}
    .bar>i{display:block;height:100%;width:0;background:#444;transition:width .2s}
    #bookBody{position:relative;width:820px;height:1160px;transform-origin:top left}.frame{position:relative}.flow{position:absolute;left:-99999px;top:0;width:660px}
    .flow .content{width:660px;height:auto;overflow:visible}
    @media (max-width:900px){
      .sheet{width:96vw;height:auto;min-height:70vh;padding:26px 22px 60px}
      .sheet .content{width:auto;height:auto;overflow:visible}
      .nav.prev{left:4px}.nav.next{right:4px}
    }
    """
    js = r"""
(function(){
  var blocks = JSON.parse(document.getElementById('blocks').textContent);
  var MAXH = 996;
  var stage = document.getElementById('stage');
  var flow = document.getElementById('flow');
  var body = document.getElementById('bookBody');
  var counter = document.getElementById('counter');
  var barFill = document.getElementById('barFill');
  var prevBtn = document.getElementById('prevBtn');
  var nextBtn = document.getElementById('nextBtn');
  var sheets = [];
  var cur = null;

  function newInner(){
    var d = document.createElement('div');
    d.className = 'content';
    flow.appendChild(d);
    return d;
  }
  function newPage(){
    cur = newInner();
    return cur;
  }
  function makePara(text, page){
    var p = document.createElement('p');
    p.appendChild(document.createTextNode(text));
    if (page) {
      var s = document.createElement('span');
      s.className = 'page-mark';
      s.textContent = 'p.' + page;
      p.appendChild(s);
    }
    return p;
  }
  function makeBlock(b){
    if (b.type === 'heading'){
      var h = document.createElement('h2');
      h.textContent = b.text;
      return h;
    }
    if (b.type === 'paragraph'){
      var pages = b.pages || [b.page];
      var mark = pages.length === 1 ? ('p.' + pages[0]) : ('p.' + pages[0] + '–' + pages[pages.length-1]);
      return makePara(b.text, mark.replace('p.',''));
    }
    if (b.type === 'figure'){
      var fig = document.createElement('figure');
      var img = document.createElement('img');
      img.src = b.src;
      img.alt = b.displayCaption || '';
      fig.appendChild(img);
      var cap = document.createElement('figcaption');
      cap.appendChild(document.createTextNode(b.displayCaption || ''));
      if (b.originalNumber){
        var o = document.createElement('span');
        o.className = 'orig';
        o.textContent = '原书 ' + b.originalNumber;
        cap.appendChild(o);
      }
      fig.appendChild(cap);
      return fig;
    }
    var d = document.createElement('div');
    d.textContent = b.text || '';
    return d;
  }
  function over(){
    return cur.scrollHeight > MAXH;
  }
  function findFit(text){
    var lo = 1, hi = text.length, best = 0;
    while (lo <= hi){
      var mid = (lo + hi) >> 1;
      var test = makePara(text.slice(0, mid), '');
      cur.appendChild(test);
      var bad = over();
      cur.removeChild(test);
      if (!bad){ best = mid; lo = mid + 1; } else { hi = mid - 1; }
    }
    return best;
  }
  function flowParagraph(text, pages){
    var rest = text;
    var first = true;
    while (rest.length){
      var mark = first ? (pages.length === 1 ? String(pages[0]) : pages[0] + '–' + pages[pages.length-1]) : '';
      var el = makePara(rest, mark);
      cur.appendChild(el);
      if (!over()){ return; }
      cur.removeChild(el);
      var fit = findFit(rest);
      if (fit <= 0){
        if (cur.childNodes.length === 0){
          var forced = makePara(rest, '');
          forced.style.fontSize = '17px';
          cur.appendChild(forced);
          return;
        }
        newPage();
        continue;
      }
      cur.appendChild(makePara(rest.slice(0, fit), first ? mark : ''));
      rest = rest.slice(fit);
      first = false;
      newPage();
    }
  }

  function build(){
  newPage();
  for (var i = 0; i < blocks.length; i++){
    var b = blocks[i];
    if (b.type === 'paragraph'){
      flowParagraph(b.text, b.pages || [b.page]);
      continue;
    }
    var el = makeBlock(b);
    cur.appendChild(el);
    if (over()){
      cur.removeChild(el);
      newPage();
      cur.appendChild(el);
    }
  }

  // 把测量出来的内容分发到白色纸张上
  var inners = Array.prototype.slice.call(flow.children);
  var bookTitle = document.getElementById('bookTitle').textContent;
  var chapterTitle = document.getElementById('chapterTitle').textContent;
  inners.forEach(function(inner, idx){
    var sheet = document.createElement('div');
    sheet.className = 'sheet';
    var running = document.createElement('div');
    running.className = 'running';
    var rl = document.createElement('span'); rl.textContent = bookTitle;
    var rr = document.createElement('span'); rr.textContent = chapterTitle;
    running.appendChild(rl); running.appendChild(rr);
    var holder = document.createElement('div');
    holder.className = 'content';
    holder.appendChild(inner);
    var pageno = document.createElement('div');
    pageno.className = 'pageno';
    pageno.textContent = '— ' + (idx + 1) + ' —';
    sheet.appendChild(running); sheet.appendChild(holder); sheet.appendChild(pageno);
    body.appendChild(sheet);
    sheets.push(sheet);
  });

  // 扉页
  var titleSheet = document.createElement('div');
  titleSheet.className = 'sheet is-active';
  titleSheet.innerHTML = '<div class="titlepage"><div class="bk">' + bookTitle + '</div>' +
    '<h1 id="tpTitle"></h1><div class="meta" id="tpMeta"></div></div>';
  titleSheet.querySelector('#tpTitle').textContent = chapterTitle;
  titleSheet.querySelector('#tpMeta').textContent = document.getElementById('metaLine').textContent;
  body.insertBefore(titleSheet, body.firstChild);
  sheets.unshift(titleSheet);

  var page = 0;
  function show(n){
    n = Math.max(0, Math.min(sheets.length - 1, n));
    sheets[page].classList.remove('is-active');
    page = n;
    sheets[page].classList.add('is-active');
    counter.textContent = (page === 0 ? '扉页' : ('第 ' + page + ' 页 / 共 ' + (sheets.length - 1) + ' 页'));
    barFill.style.width = (sheets.length > 1 ? (page / (sheets.length - 1) * 100) : 0) + '%';
    prevBtn.disabled = page === 0;
    nextBtn.disabled = page === sheets.length - 1;
  }
  prevBtn.addEventListener('click', function(){ show(page - 1); });
  nextBtn.addEventListener('click', function(){ show(page + 1); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'ArrowLeft' || e.key === 'PageUp'){ show(page - 1); e.preventDefault(); }
    if (e.key === 'ArrowRight' || e.key === 'PageDown' || e.key === ' '){ show(page + 1); e.preventDefault(); }
  });
  document.getElementById('stage').addEventListener('click', function(e){
    if (e.target.closest('.nav')) return;
    var r = stage.getBoundingClientRect();
    if (e.clientX - r.left < r.width * 0.25) show(page - 1);
    else if (e.clientX - r.left > r.width * 0.75) show(page + 1);
  });
  function fit(){
    var stageH = stage.clientHeight - 24;
    var stageW = stage.clientWidth - 150;
    var k = Math.min(stageH / 1160, stageW / 820, 1);
    if (!(k > 0)) k = 1;
    var frame = document.getElementById('frame');
    frame.style.width = (820 * k) + 'px';
    frame.style.height = (1160 * k) + 'px';
    body.style.transform = 'scale(' + k + ')';
  }
  window.addEventListener('resize', fit);

  show(0);
  fit();
  }

  var srcs = [];
  blocks.forEach(function(b){ if (b.type === 'figure') srcs.push(b.src); });
  Promise.all(srcs.map(function(s){
    return new Promise(function(res){ var im = new Image(); im.onload = res; im.onerror = res; im.src = s; });
  })).then(build);
})();
"""
    meta_line = (f"原书 p.{book['pageRange'][0]}–{book['pageRange'][1]} · 正文 {book['stats']['chars']} 字 · "
                 f"插图 {book['stats']['figures']} 幅 · 图注配对 {book['stats']['paired']}")
    html = (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{book['title']} · 翻页阅读</title><style>{css}</style></head><body>"
        "<div class=\"topbar\"><span><b id=\"bookTitle\">" + book["bookTitle"] + "</b>"
        " · 统一排版样章</span><span>← → 翻页 · 点两侧也可翻页</span></div>"
        "<div class=\"stage\" id=\"stage\">"
        "<button class=\"nav prev\" id=\"prevBtn\" aria-label=\"上一页\">‹</button>"
        "<div class=\"frame\" id=\"frame\"><div id=\"bookBody\"></div></div>"
        "<button class=\"nav next\" id=\"nextBtn\" aria-label=\"下一页\">›</button>"
        "</div>"
        "<div class=\"bottom\"><div class=\"counter\" id=\"counter\"></div>"
        "<div class=\"bar\"><i id=\"barFill\"></i></div></div>"
        f"<div id=\"chapterTitle\" style=\"display:none\">{book['title']}</div>"
        f"<div id=\"metaLine\" style=\"display:none\">{meta_line}</div>"
        "<div class=\"flow\" id=\"flow\"></div>"
        "<script id=\"blocks\" type=\"application/json\">" + blocks_json + "</script>"
        "<script>" + js + "</script>"
        "</body></html>"
    )
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)


def main():
    global SCALE, HIRES_SCALE
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("start", type=int)
    ap.add_argument("end", type=int)
    ap.add_argument("out")
    ap.add_argument("--title", default="")
    ap.add_argument("--book", default="")
    ap.add_argument("--text-layer", choices=["auto", "on", "off"], default="auto")
    ap.add_argument("--scale", type=float, default=SCALE)
    args = ap.parse_args()
    SCALE = float(args.scale)
    HIRES_SCALE = max(SCALE + 0.5, 3.0)   # 裁图倍率：不再固定 4.0，省渲染/编码时间

    t0 = time.time()
    out = args.out
    cache_dir = os.path.join(out, "pages")
    figs_dir = os.path.join(out, "assets")
    os.makedirs(cache_dir, exist_ok=True)
    os.makedirs(figs_dir, exist_ok=True)

    pdf = pdfium.PdfDocument(args.pdf)
    try:
        pw, ph = pdf[0].get_size()
        max_dim = max(pw, ph)
        if max_dim > 0 and max_dim * args.scale > 4200:
            # 超大页面：把渲染像素长边压到约 4200，避免单页渲染/OCR 耗时爆炸
            SCALE = max(1.0, round(4200.0 / max_dim, 2))
            HIRES_SCALE = max(SCALE + 0.5, round(5000.0 / max_dim, 2))
            print("页面超大（%.0f×%.0f pt）→ 自动使用 %.2f 倍渲染" % (pw, ph, SCALE))
    except Exception:
        pass
    use_text = args.text_layer == "on" or (args.text_layer == "auto" and has_text_layer(args.pdf))
    def _make_engine():
        cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "rapidocr_threads.yaml")
        if os.path.exists(cfg):     # 多进程并发时限制 ONNX 线程，避免线程争抢拖慢整体
            try:
                return RapidOCR(config_path=cfg)
            except Exception:
                pass
        return RapidOCR()

    engine = None if use_text else _make_engine()
    print("模式：", "文字层直读（免 OCR）" if use_text else "OCR 识别")
    fig_seq = {"n": 0}
    all_blocks = []
    printed = {}
    ocr_texts = []

    print(f"[1/4] 渲染 + OCR：{args.start}-{args.end} 页")
    page_lines = []
    for n in range(args.start, args.end + 1):
        p2 = render_page(pdf, n, SCALE, cache_dir)
        p4 = render_page(pdf, n, HIRES_SCALE, cache_dir)
        if use_text:
            lines = text_layer_lines(args.pdf, n, SCALE, os.path.join(cache_dir, f"page_{n:03d}_text.json"))
        else:
            lines = ocr_page_lines(engine, p2, os.path.join(cache_dir, f"page_{n:03d}_ocr2.json"))
        page_lines.append((n, lines, Image.open(p2).size[1] if isinstance(p2, str) else p2.size[1]))
    # 统计跨页重复的页眉/页脚（小页边距的书，第一行正文常被当成页眉删掉）
    top_c, bot_c = Counter(), Counter()
    for _n, _lines, _H in page_lines:
        for l in _lines:
            t = l["text"].strip()
            if l["y1"] < _H * 0.12:
                top_c[t] += 1
            elif l["y0"] > _H * 0.86:
                bot_c[t] += 1
    need = max(3, int(len(page_lines) * 0.15))
    header_set = {t for t, c in top_c.items() if c >= need and 0 < len(t) <= 30}
    footer_set = {t for t, c in bot_c.items() if c >= need and 0 < len(t) <= 30}
    print(f"   页眉候选 {len(header_set)} 条 · 页脚候选 {len(footer_set)} 条（跨页重复才删）")
    for n, lines, _H in page_lines:
        flow, figs, pp = parse_page(pdf, n, lines, cache_dir, cache_dir, figs_dir, fig_seq, 40.0, header_set, footer_set)
        all_blocks.extend(flow)
        if pp:
            printed[n] = pp
        ocr_texts.append("\n".join(l["text"] for l in lines))
        print(f"    p{n}: {len(lines)} 行 · 段落 {sum(1 for b in flow if b['type']=='paragraph')} · 图 {len(figs)} · 页码 {pp}")

    print("[2/4] 跨页合并…")
    blocks = merge_cross_page(all_blocks)

    # 标题排版：章号后补空格（第1章古代埃及的建筑 → 第1章 古代埃及的建筑）
    for b in blocks:
        if b["type"] == "heading":
            b["text"] = re.sub(r"^(第\s?[0-9一二三四五六七八九十百]+\s?[章篇节])(?=\S)", r"\1 ", b["text"])

    # 修正 OCR 漏读页号首位数字（例如 14 被读成 4）
    if printed:
        diffs = Counter(n - p for n, p in printed.items())
        offset = diffs.most_common(1)[0][0]
        for n, p in list(printed.items()):
            expected = n - offset
            if p == expected:
                continue
            for k in (10, 100, 1000):
                if p + k == expected or p - k == expected:
                    printed[n] = expected
                    break

    print("[3/4] 图号重排…")
    mapping = renumber_figures(blocks)

    title = args.title or next((b["text"] for b in blocks if b["type"] == "heading"), f"p.{args.start}-{args.end}")
    book_title = args.book or os.path.splitext(os.path.basename(args.pdf))[0]
    # 去掉与章标题重复的第一个 heading，避免预览里出现两次标题
    def norm_t(s):
        return re.sub(r"\s+", "", s)
    if blocks and blocks[0]["type"] == "heading" and norm_t(blocks[0]["text"]) == norm_t(title):
        blocks = blocks[1:]
    chars = sum(len(b["text"]) for b in blocks if b["type"] == "paragraph")
    figs = [b for b in blocks if b["type"] == "figure"]
    chapters = split_chapters(blocks, title)
    book = {
        "bookTitle": book_title,
        "title": title,
        "source": os.path.basename(args.pdf),
        "pageRange": [args.start, args.end],
        "printedPages": {str(k): v for k, v in printed.items()},
        "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chapters": chapters,
        "blocks": blocks,
        "figureMap": mapping,
        "stats": {
            "pages": args.end - args.start + 1,
            "chars": chars,
            "paragraphs": sum(1 for b in blocks if b["type"] == "paragraph"),
            "figures": len(figs),
            "paired": sum(1 for f in figs if f.get("caption")),
            "chapters": len(chapters),
        },
    }
    with open(os.path.join(out, "book.json"), "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    with open(os.path.join(out, "book_raw.json"), "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)

    print("[4/4] 生成预览…")
    build_book_preview(book, os.path.join(out, "preview.html"))

    report = {
        "pages": args.end - args.start + 1,
        "seconds": round(time.time() - t0, 1),
        "chars": chars,
        "paragraphs": book["stats"]["paragraphs"],
        "figures": len(figs),
        "paired": book["stats"]["paired"],
        "figureMapSize": len(mapping),
        "printedPageNumbers": len(printed),
        "wordCountPerPage": round(chars / (args.end - args.start + 1), 1),
        "mode": "text" if use_text else "ocr",
    }
    with open(os.path.join(out, "report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print("完成：", json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
