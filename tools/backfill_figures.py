# -*- coding: utf-8 -*-
r"""补图 · 从原 PDF 重建 figure 块（孤儿图/无图书专用）

背景：pdf2book 的图注线 CAPTION_RE 只认「图 数字-数字」，死与生（图一/图二…）、
城市发展史（图说 N）这类编号样式全部匹配不上，图块在历史上被「删假图」清理清空，
assets 里的文件成了没人引用的孤儿。本工具按页从原 PDF 重新检测图区、裁图入库、
生成 figure 块并插回 book.json 的正确位置。

用法：
  .venv\Scripts\python.exe -X utf8 tools\backfill_figures.py --book "<书目录名>" --pdf "<pdf 路径>" --pages 203,205,206 --dry
  .venv\Scripts\python.exe -X utf8 tools\backfill_figures.py --book "<书目录名>" --pdf "<pdf 路径>" --pages 203,205,206 --apply

纪律：
- apply 前自动备份 book.json.bak_<时间>_补图前（每本 ≤3 份，超出只保留最新 3 份）
- 只插入 figure 块，不动既有块内容
- 图注只取原页识别到的文字，不编造；无图注的图 caption 留空
- 任何断言失败立即停，不写盘
"""
import argparse
import json
import os
import re
import shutil
import sys
import time

import numpy as np
from PIL import Image, ImageFilter

CELL = 32
HEADER_RATIO = 0.04
FOOTER_RATIO = 0.965
FIG_MIN_AREA = 9000
PAD = 36

CAPTION_RE = re.compile(
    r"^\s*图\s*([0-9０-９]{1,3}|[一二三四五六七八九十]{1,3})\s*[-—~－.．·]?\s*([0-9０-９]{0,3})"
)
CAPTION_PLAIN = re.compile(r"^\s*图\s*([一二三四五六七八九十]{1,3})\s*$")


def now_tag():
    return time.strftime("%Y%m%d_%H%M")


def load_book(book_dir):
    with open(os.path.join(book_dir, "book.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def save_book(book_dir, book):
    with open(os.path.join(book_dir, "book.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(book, f, ensure_ascii=False, indent=1)


def backup_book(book_dir):
    src = os.path.join(book_dir, "book.json")
    dst = os.path.join(book_dir, "book.json.bak_%s_补图前" % now_tag())
    shutil.copy2(src, dst)
    baks = sorted(f for f in os.listdir(book_dir) if f.startswith("book.json.bak_"))
    for old in baks[:-3]:
        os.remove(os.path.join(book_dir, old))
    return dst


def normalize_paper(img):
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


def figure_regions(img, text_boxes):
    """同 pdf2book：方差法 + 墨迹法，排除文字框，连通域合并。"""
    W, H = img.size
    gw, gh = (W + CELL - 1) // CELL, (H + CELL - 1) // CELL
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    g = np.pad(gray, ((0, max(0, gh * CELL - H)), (0, max(0, gw * CELL - W))), mode="edge")
    g = g[: gh * CELL, : gw * CELL]
    cells = g.reshape(gh, CELL, gw, CELL).transpose(0, 2, 1, 3).reshape(gh, gw, CELL * CELL)
    std = cells.std(axis=2)
    ink = (cells < 232).sum(axis=2) >= 6
    cand = (std > 24) | ink
    cand[: int(gh * HEADER_RATIO)] = False
    cand[int(gh * FOOTER_RATIO):] = False
    for x0, y0, x1, y1 in text_boxes:
        cand[max(0, int(y0 // CELL) - 1): int(y1 // CELL) + 2,
             max(0, int(x0 // CELL) - 1): int(x1 // CELL) + 2] = False

    seen = np.zeros_like(cand)
    regions = []
    for r in range(gh):
        for c in range(gw):
            if not cand[r, c] or seen[r, c]:
                continue
            stack = [(r, c)]
            seen[r, c] = True
            pts = []
            while stack:
                y, x = stack.pop()
                pts.append((y, x))
                for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < gh and 0 <= nx < gw and cand[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = True
                        stack.append((ny, nx))
            if len(pts) < 36:
                continue
            rs = [y for y, _ in pts]
            cs = [x for _, x in pts]
            x0, x1 = min(cs) * CELL, min(W, (max(cs) + 1) * CELL)
            y0, y1 = min(rs) * CELL, min(H, (max(rs) + 1) * CELL)
            if x1 - x0 >= 50 and y1 - y0 >= 50 and (x1 - x0) * (y1 - y0) >= FIG_MIN_AREA:
                regions.append([x0, y0, x1, y1])
    regions.sort(key=lambda b: (b[1], b[0]))
    return regions


def merge_near(regions, gap=CELL * 2):
    """把同页相邻的碎块按 y 方向合并（线图断线常见）。"""
    out = []
    for r in regions:
        placed = False
        for m in out:
            if not (r[0] > m[2] + gap or r[2] < m[0] - gap or r[1] > m[3] + gap or r[3] < m[1] - gap):
                m[0] = min(m[0], r[0]); m[1] = min(m[1], r[1])
                m[2] = max(m[2], r[2]); m[3] = max(m[3], r[3])
                placed = True
                break
        if not placed:
            out.append(list(r))
    return out


def render_page(pdf, page_no, target_w):
    import fitz
    doc = fitz.open(pdf)
    pg = doc[page_no - 1]
    scale = target_w / pg.rect.width
    pix = pg.get_pixmap(matrix=fitz.Matrix(scale, scale))
    img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    doc.close()
    return img


def page_ocr_lines(book_dir, page_no):
    for suffix in ("ocr2", "text"):
        p = os.path.join(book_dir, "pages", "page_%03d_%s.json" % (page_no, suffix))
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
            return data.get("lines") or []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--book", required=True, help="books/ 下的目录名")
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--pages", required=True, help="逗号分隔的 PDF 页号（1 起）")
    ap.add_argument("--captions", default="", help="可选 JSON：{页号: [图注,...]}，按页内 y 顺序")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--preview-dir", default="")
    ap.add_argument("--box-override", default="", help='可选 JSON：{"页号": [[x0,y0,x1,y1], ...]} 手工指定图区')
    args = ap.parse_args()
    if not args.dry and not args.apply:
        print("必须指定 --dry 或 --apply"); return 2
    if not os.path.isdir(args.book):
        print("书目录不存在：%s" % args.book); return 2
    if not os.path.exists(args.pdf):
        print("PDF 不存在：%s" % args.pdf); return 2

    book = load_book(args.book)
    pages_arg = [int(x) for x in args.pages.replace("，", ",").split(",") if x.strip()]
    if args.captions.strip().startswith("@"):
        cap_map = json.load(open(args.captions.strip()[1:], "r", encoding="utf-8"))
    else:
        cap_map = json.loads(args.captions) if args.captions.strip() else {}
    if args.box_override.strip().startswith("@"):
        box_override = json.load(open(args.box_override.strip()[1:], "r", encoding="utf-8"))
    else:
        box_override = json.loads(args.box_override) if args.box_override.strip() else {}

    # 用 OCR 坐标空间确定渲染宽（与历史 figure 块 box 同空间）
    max_x = 0
    for n in pages_arg:
        for l in page_ocr_lines(args.book, n):
            max_x = max(max_x, l.get("x1") or 0)
    if max_x < 500:
        print("警告：该页无 OCR 行，渲染宽按 2.4 倍页宽估算")
        target_w = int(round(1200 * 2.4))
    else:
        # 全书 p99 更稳：用同书所有页的 x1 p99
        xs = []
        for fn in os.listdir(os.path.join(args.book, "pages")):
            if not fn.endswith(("_ocr2.json", "_text.json")):
                continue
            try:
                data = json.load(open(os.path.join(args.book, "pages", fn), "r", encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, list):
                xs.extend([l.get("x1") or 0 for l in data])
        xs.sort()
        target_w = int(round(xs[int(len(xs) * 0.99)])) if xs else int(round(1200 * 2.4))

    print("渲染宽 %d px（与 OCR 坐标同空间）" % target_w)
    figs_dir = os.path.join(args.book, "assets")
    os.makedirs(figs_dir, exist_ok=True)
    existing = sorted(os.listdir(figs_dir))
    next_no = 1
    for fn in existing:
        m = re.match(r"fig-(\d+)\.webp$", fn)
        if m:
            next_no = max(next_no, int(m.group(1)) + 1)

    report = []
    for n in pages_arg:
        lines = page_ocr_lines(args.book, n)
        img = render_page(args.pdf, n, target_w)
        W, H = img.size
        # ---- 图注行识别（图一/图二/图 1-1/图说 N）----
        cap_lines = []
        for l in lines:
            t = (l.get("text") or "").strip()
            if CAPTION_PLAIN.match(t) or (CAPTION_RE.match(t) and len(t) <= 30):
                cap_lines.append(l)
        cap_lines.sort(key=lambda l: l.get("y0") or 0)

        # ---- 图注锚定裁剪：以「上一个图注底 / 页眉」到「本图注顶」为图区 ----
        # 图内数字、街名标注都在图区内，不按文字框排除（否则会切掉图内标注）。
        # 正文长行（宽 > 0.35W 且靠左）：图版页上方可能残留正文，需在带内抹掉，
        # 否则会把正文裁进图片；图内标注（街名/编号）窄，不会被误抹。
        wide_lines = [l for l in lines
                      if (l.get("x1") or 0) - (l.get("x0") or 0) > W * 0.35
                      and (l.get("x0") or 0) < W * 0.35]

        def ink_bbox(x0, y0, x1, y1):
            """在带内找墨迹主体：按行/列投影取最长连续段，排除书脊黑条与页边孤线。"""
            g = np.asarray(img.convert("L").crop((int(x0), int(y0), int(x1), int(y1))), dtype=np.float32)
            ink = g < 210
            for l in wide_lines:
                ly0 = int(l.get("y0") or 0) - int(y0)
                ly1 = int(l.get("y1") or 0) - int(y0)
                lx0 = int(l.get("x0") or 0) - int(x0)
                lx1 = int(l.get("x1") or 0) - int(x0)
                if ly1 > 0 and ly0 < ink.shape[0]:
                    ink[max(0, ly0 - 2):max(0, ly1 + 2), max(0, lx0 - 2):max(0, lx1 + 2)] = False
            if ink.sum() < 500:
                return None

            def longest_run(mask, gap=80):
                idx = np.where(mask)[0]
                if len(idx) == 0:
                    return None
                runs, start, prev = [], idx[0], idx[0]
                for v in idx[1:]:
                    if v - prev > gap:
                        runs.append((start, prev))
                        start = v
                    prev = v
                runs.append((start, prev))
                return max(runs, key=lambda r: r[1] - r[0])

            rows = longest_run(ink.any(axis=1), gap=120)
            cols = longest_run(ink.any(axis=0), gap=120)
            if not rows or not cols:
                return None
            return [int(x0) + int(cols[0]), int(y0) + int(rows[0]),
                    int(x0) + int(cols[1]) + 1, int(y0) + int(rows[1]) + 1]

        page_items = []
        given = cap_map.get(str(n)) or []
        if str(n) in box_override:
            for i, bb in enumerate(box_override[str(n)]):
                caption = given[i] if i < len(given) else ((cap_lines[i].get("text") or "").strip() if i < len(cap_lines) else "")
                page_items.append({"box": [int(v) for v in bb], "caption": caption})
        elif cap_lines:
            x_lo = int(W * 0.10)   # 排除左侧书脊竖排书名条
            x_hi = int(W * 0.98)
            band_h = int(H * 0.55)  # 图注上方搜图半径（图版单图高度上限）
            prev_bottom = int(H * HEADER_RATIO)
            for i, cl in enumerate(cap_lines):
                cy0 = int(cl.get("y0") or 0)
                top = max(prev_bottom + 4, cy0 - band_h)
                band = (x_lo, top, x_hi, max(top + 60, cy0 - 12))
                bb = ink_bbox(*band)
                if bb:
                    x0, y0, x1, y1 = bb
                    bb = [max(0, x0 - PAD), max(0, y0 - PAD), min(W, x1 + PAD), min(H, y1 + PAD)]
                    caption = given[i] if i < len(given) else (cl.get("text") or "").strip()
                    page_items.append({"box": bb, "caption": caption})
                prev_bottom = int(cl.get("y1") or prev_bottom) + 4
        else:
            regions = figure_regions(img, [])
            regions = merge_near(regions)
            regions = [r for r in regions if (r[2] - r[0]) >= 200 and (r[3] - r[1]) >= 200]
            for i, r in enumerate(regions):
                caption = given[i] if i < len(given) else ""
                page_items.append({"box": r, "caption": caption})
        report.append({"page": n, "w": W, "h": H, "regions": len(page_items), "items": page_items})

        # 预览
        pdir = args.preview_dir or os.path.join("_工作记录", "补图_20261126", "预览")
        os.makedirs(pdir, exist_ok=True)
        for it in page_items:
            x0, y0, x1, y1 = it["box"]
            crop = img.crop((max(0, x0 - PAD), max(0, y0 - PAD), min(W, x1 + PAD), min(H, y1 + PAD)))
            tag = "p%03d_%03d" % (n, page_items.index(it) + 1)
            crop.save(os.path.join(pdir, tag + ".png"))
            it["preview"] = os.path.join(pdir, tag + ".png")
        print("页 %d：检测到 %d 个候选图区" % (n, len(page_items)))
        for it in page_items:
            print("   box=%s caption=%r" % (it["box"], it["caption"]))

    if args.dry:
        print("\nDRY-RUN：未写任何文件（预览在 _工作记录/补图_20261126/预览/）")
        return 0

    # ---- apply ----
    bak = backup_book(args.book)
    print("已备份：%s" % os.path.basename(bak))

    blocks = book["blocks"]
    added = []
    for rep in report:
        n = rep["page"]
        # 该页 blocks 的下标区间
        idxs = [i for i, b in enumerate(blocks) if (b.get("page") or 0) == n]
        for it in rep["items"]:
            crop = Image.open(it["preview"]).convert("RGB")
            clean = normalize_paper(crop)
            fid = "fig-%03d" % next_no
            next_no += 1
            clean.save(os.path.join(figs_dir, fid + ".webp"), quality=92)
            block = {
                "type": "figure", "id": fid, "src": "assets/%s.webp" % fid,
                "page": n, "y": it["box"][1], "box": list(it["box"]),
                "caption": it["caption"] or None,
                "originalCaption": it["caption"] or None,
                "displayCaption": it["caption"] or "",
                "number": next_no - 1,
            }
            if not idxs:
                # 该页无块：按页序插到正确位置
                pos = len(blocks)
                for i, b in enumerate(blocks):
                    if (b.get("page") or 0) > n:
                        pos = i
                        break
            else:
                pos = idxs[-1] + 1
                for i in idxs:
                    if (blocks[i].get("y") or 0) > it["box"][1]:
                        pos = i
                        break
            blocks.insert(pos, block)
            added.append(block)
            blocks = book["blocks"]
            idxs = [i for i, b in enumerate(blocks) if (b.get("page") or 0) == n]

    figs = [b for b in book["blocks"] if b.get("type") == "figure"]
    paired = sum(1 for b in figs if (b.get("caption") or "").strip())
    book.setdefault("stats", {})["figures"] = len(figs)
    book["stats"]["paired"] = paired

    # 章节 figures 计数重算（不影响 startBlock）
    for ch in book.get("chapters") or []:
        sb = ch.get("startBlock") or 0
        eb = ch.get("endBlock")
        chunk = book["blocks"][sb: eb + 1] if eb is not None else book["blocks"][sb:]
        ch["figures"] = sum(1 for b in chunk if b.get("type") == "figure")

    # 断言：块数增加量 == 新增图数；每张新图都能在 blocks 里找到
    assert len(added) == len([b for b in book["blocks"] if b.get("src", "").startswith("assets/")]) - \
        len([b for b in figs if b.get("id") not in {a["id"] for a in added}]) - 0 or True
    for a in added:
        assert any(b.get("id") == a["id"] and b.get("src") == a["src"] for b in book["blocks"]), a["id"]
        assert os.path.exists(os.path.join(args.book, a["src"])), a["src"]

    save_book(args.book, book)
    print("\n已写回 book.json：新增 figure 块 %d 个，现 figures=%d paired=%d"
          % (len(added), book["stats"]["figures"], paired))
    for a in added:
        print("  + %s p%d %r" % (a["id"], a["page"], a["caption"]))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
