# -*- coding: utf-8 -*-
"""table_grid.py —— 用表格线（而非 x 聚类）恢复表格结构。

思路：
  1. 页面图二值化，找「长水平墨迹连续段」= 横线、「长垂直连续段」= 竖线
  2. 横线按 x 区间相容 + 组内大间隙再切 分组，每组一张表
  3. 列边界优先取贯穿表高的竖线；竖线不足时用「碎片中心聚类」（多层表头/三线表）
  4. 单元格 = 中心落在「行×列」格内的 OCR 文字；仅在同一横线带内合并多行单元格
  5. 表题行自动转 caption 并作为分表边界

用法：
    .venv\\Scripts\\python.exe tools\\table_grid.py <书名> probe <页码...>
    .venv\\Scripts\\python.exe tools\\table_grid.py <书名> scan <起页> <止页>
"""
from paths import BOOKS
import io
import json
import os
import re
import sys
import time
from collections import Counter

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pymupdf

BOOKS = BOOKS
HAN = re.compile(r"[\u4e00-\u9fff]")
norm = lambda s: "".join(HAN.findall(s or ""))


def resolve(arg):
    key = re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", arg or "")
    hit = [x for x in sorted(os.listdir(BOOKS)) if key and key in re.sub(r"[\s\u3000·…．\-—_（）()，,]", "", x)]
    if len(hit) == 1:
        return os.path.join(BOOKS, hit[0])
    raise SystemExit("匹配 %d 本: %s" % (len(hit), hit[:5]))


def longest_run(mask, gap=4):
    idx = np.where(mask)[0]
    if len(idx) == 0:
        return (0, 0, 0)
    best_s = best_e = int(idx[0])
    s = prev = int(idx[0])
    for v in idx[1:]:
        v = int(v)
        if v - prev <= gap + 1:
            prev = v
        else:
            if prev - s > best_e - best_s:
                best_s, best_e = s, prev
            s = v
            prev = v
    if prev - s > best_e - best_s:
        best_s, best_e = s, prev
    return (best_s, best_e, best_e - best_s + 1)


def find_rules(binary, axis, min_len, gap=4):
    """axis=1 逐行找横线；axis=0 逐列找竖线。返回 [(位置, 起, 止)]"""
    out = []
    n = binary.shape[0] if axis == 1 else binary.shape[1]
    for i in range(n):
        line = binary[i, :] if axis == 1 else binary[:, i]
        s, e, ln = longest_run(line, gap)
        if ln >= min_len:
            out.append((i, s, e))
    merged = []
    for pos, s, e in out:
        if merged and pos - merged[-1][0][-1] <= 2:
            merged[-1][0].append(pos)
            merged[-1][1] = min(merged[-1][1], s)
            merged[-1][2] = max(merged[-1][2], e)
        else:
            merged.append([[pos], s, e])
    return [(int(np.mean(g)), s, e) for g, s, e in merged]


def cluster(vals, tol):
    vals = sorted(vals)
    groups = []
    for v in vals:
        if groups and v - groups[-1][-1] <= tol:
            groups[-1].append(v)
        else:
            groups.append([v])
    return [float(np.mean(g)) for g in groups]


def detect_tables(binary, W, H):
    h = find_rules(binary, 1, max(150, int(W * 0.13)))
    if len(h) < 3:
        return []
    h.sort()
    groups = []
    for item in h:
        best, bestscore = None, -1
        for g in groups:
            ref = g[-1]
            ov = min(ref[2], item[2]) - max(ref[1], item[1])
            wide = max(1, min(ref[2] - ref[1], item[2] - item[1]))
            score = ov / float(wide)
            if score >= 0.62 and item[0] - ref[0] <= 420 and score > bestscore:
                best, bestscore = g, score
        if best is not None:
            best.append(item)
        else:
            groups.append([item])
    split = []
    for g in groups:
        if len(g) < 2:
            split.append(g)
            continue
        gaps = [g[i + 1][0] - g[i][0] for i in range(len(g) - 1)]
        med = sorted(gaps)[len(gaps) // 2]
        cur = [g[0]]
        for i, item in enumerate(g[1:], 1):
            if gaps[i - 1] > max(200, med * 3):
                split.append(cur)
                cur = []
            cur.append(item)
        split.append(cur)
    out = []
    for g in split:
        if len(g) < 3:
            continue
        ys = [p for p, _, _ in g]
        y0, y1 = min(ys), max(ys)
        ss = sorted(s for _, s, _ in g)
        ee = sorted(e for _, _, e in g)
        x0, x1 = ss[len(ss) // 2], ee[len(ee) // 2]
        if y1 - y0 < 50 or x1 - x0 < 150:
            continue
        sub = binary[y0:y1 + 1, :]
        v = find_rules(sub, 0, max(40, int((y1 - y0) * 0.35)))
        vlines = sorted({p for p, _, _ in v if x0 - 25 <= p <= x1 + 25})
        out.append({"bbox": (x0, y0, x1, y1), "hlines": sorted(g), "vlines": vlines,
                    "ruled": len(vlines) >= 2})
    return out


def cells_from_grid(L, det, lh):
    """返回 [(rows, cols, title)]"""
    x0, y0, x1, y1 = det["bbox"]
    inner = [c for c in det["vlines"] if x0 + 3 < c < x1 - 3]
    cols = None
    if det.get("ruled", True) and len(inner) >= 3:
        bounds = sorted(set([int(v) for v in [x0 - 6] + inner + [x1 + 6]]))
        cand = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        cols = [c for c in cand if c[1] - c[0] >= 16]
    inside = [l for l in L if (l.get("text") or "").strip()
              and x0 - 8 <= (l["x0"] + l["x1"]) / 2 <= x1 + 8
              and y0 - 4 <= (l["y0"] + l["y1"]) / 2 <= y1 + 4]
    if not inside:
        return []
    line_h = max(12, int(lh))
    if cols is None:
        # 多层表头/三线表：用碎片中心聚类定列
        centers = sorted((l["x0"] + l["x1"]) / 2.0 for l in inside)
        groups = []
        for c in centers:
            if groups and c - groups[-1][-1] <= 42:
                groups[-1].append(c)
            else:
                groups.append([c])
        keep = [g for g in groups if len(g) >= 3]
        if len(keep) < 2:
            keep = groups
        bounds = [x0 - 6]
        for i in range(len(keep) - 1):
            bounds.append((keep[i][-1] + keep[i + 1][0]) / 2.0)
        bounds.append(x1 + 6)
        bounds = [int(v) for v in sorted(set(bounds))]
        cand = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
        cols = [c for c in cand if c[1] - c[0] >= 30]
        if len(cols) < 2:
            return []
    full_h = sorted([int(p) for p, s, e in det["hlines"] if e - s >= (x1 - x0) * 0.72])

    def band_of(cy):
        k = 0
        for i, p in enumerate(full_h):
            if cy > p:
                k = i + 1
        return k

    ys = sorted({round((l["y0"] + l["y1"]) / 2, 1) for l in inside})
    rowc = cluster(ys, max(9, int(line_h * 0.55)))
    grid, meta = [], []
    for l in sorted(inside, key=lambda x: x["y0"]):
        cy = (l["y0"] + l["y1"]) / 2
        cx = (l["x0"] + l["x1"]) / 2
        ri = min(range(len(rowc)), key=lambda k: abs(rowc[k] - cy)) if rowc else 0
        ci = len(cols) - 1
        for k, (a, b) in enumerate(cols):
            if a <= cx <= b:
                ci = k
                break
        while len(grid) <= ri:
            grid.append([""] * len(cols))
            meta.append(None)
        if meta[ri] is None:
            meta[ri] = [band_of(cy), int(round(rowc[ri]))]
        t = (l.get("text") or "").strip()
        grid[ri][ci] = (grid[ri][ci] + " " + t).strip() if grid[ri][ci] else t
    # 同一横线带内、只占一列的续行并入上一行
    merged, mmeta = [], []
    for ri, r in enumerate(grid):
        filled = [k for k, c in enumerate(r) if c]
        if merged and len(filled) == 1 and mmeta and mmeta[-1][0] == meta[ri][0]:
            k = filled[0]
            if merged[-1][k] and meta[ri][1] - mmeta[-1][1] < 1.9 * line_h:
                merged[-1][k] = (merged[-1][k] + " " + r[k]).strip()
                continue
        merged.append(list(r))
        mmeta.append(list(meta[ri]))
    grid = merged
    TIT = re.compile(r"^\s*[（(]?[上下左右]?[：:]?\s*(表|光表)\s*[0-9０-９]+([.．\-—~～][0-9０-９]+)*")
    tables, cur, title = [], [], ""
    for r in grid:
        joined = "".join(r)
        if any(TIT.match(c.strip()) for c in r if c):
            if cur:
                tables.append((cur, title))
            nums = re.findall(r"(?:表|光表)\s*[0-9０-９]+(?:[.．\-—~～][0-9０-９]+)*", joined)
            rest = re.sub(r"[（(]?[上下左右]?[：:]?\s*(?:表|光表)\s*[0-9０-９]+(?:[.．\-—~～][0-9０-９]+)*[）)]?", "", joined).strip()
            title = ((nums[0].replace(" ", "") if nums else "") + " " + rest).strip()
            cur = []
        else:
            cur.append(r)
    if cur:
        tables.append((cur, title))
    return [(rows, cols, title) for rows, title in tables if rows]


def ocr_cells(doc, p, det, tmp, eng=None, max_cells=260):
    """按「行带 × 列带」逐格重新 OCR：解决同一行数字被 OCR 粘成一个碎片的问题。
    需要表格同时有横线与竖线。返回 rows 或 None。"""
    x0, y0, x1, y1 = det["bbox"]
    inner_v = sorted([c for c in det["vlines"] if x0 + 3 < c < x1 - 3])
    inner_h = sorted([p2 for p2, s, e in det["hlines"] if e - s >= (x1 - x0) * 0.6 and y0 + 2 < p2 < y1 - 2])
    if len(inner_v) < 2 or len(inner_h) < 2:
        return None
    cb = [x0] + inner_v + [x1]
    rb = [y0] + inner_h + [y1]
    cols = [(cb[i], cb[i + 1]) for i in range(len(cb) - 1)]
    rowsb = [(rb[i], rb[i + 1]) for i in range(len(rb) - 1)]
    rowsb = [r for r in rowsb if r[1] - r[0] >= 10]
    if len(cols) * len(rowsb) > max_cells or len(cols) < 2 or len(rowsb) < 2:
        return None
    if eng is None:
        import pdf2book as P
        eng = P.RapidOCR()
    from PIL import Image as I
    out = []
    for (ry0, ry1) in rowsb:
        line = []
        for (cx0, cx1) in cols:
            box = (max(0, cx0 - 3), max(0, ry0 - 3), min(4000, cx1 + 3), min(4000, ry1 + 3))
            pm = doc[p - 1].get_pixmap(matrix=pymupdf.Matrix(4.0, 4.0),
                                      clip=pymupdf.Rect(box[0] / 3.0, box[1] / 3.0, box[2] / 3.0, box[3] / 3.0))
            f = os.path.join(tmp, "cell.png")
            pm.save(f)
            im = I.open(f)
            if im.width < 40:
                im = im.resize((40, max(10, int(im.height * 40.0 / im.width))), I.LANCZOS)
            res, _ = eng(np.asarray(im.convert("RGB")))
            txt = " ".join((t or "").strip() for _, t, _ in (res or []) if (t or "").strip())
            line.append(txt.strip())
        out.append(line)
    return out


def page_lines_and_image(doc, p, tmp):
    png = os.path.join(tmp, "g_%03d.png" % p)
    if not os.path.exists(png):
        pm = doc[p - 1].get_pixmap(matrix=pymupdf.Matrix(3.0, 3.0))
        pm.save(png)
    img = Image.open(png).convert("L")
    return img, np.asarray(img, dtype=np.uint8)


def load_cache(book_dir):
    pages = os.path.join(book_dir, "pages")
    out = {}
    for f in sorted(os.listdir(pages)):
        m = re.match(r"page_(\d+)_ocr2\.json", f)
        if not m:
            continue
        out[int(m.group(1))] = json.load(io.open(os.path.join(pages, f), encoding="utf-8"))
    return out


def main():
    mode = sys.argv[2] if len(sys.argv) > 2 else "probe"
    d = resolve(sys.argv[1])
    lbp = load_cache(d)
    pdf = os.path.join(os.path.join(ROOT, "inbox"), os.path.basename(d) + ".pdf")
    if not os.path.exists(pdf):
        raise SystemExit("缺 PDF: " + pdf)
    doc = pymupdf.open(pdf)
    tmp = os.path.join(d, "_grid_imgs")
    os.makedirs(tmp, exist_ok=True)
    if mode == "probe":
        for p in [int(x) for x in sys.argv[3:]] or [39]:
            img, a = page_lines_and_image(doc, p, tmp)
            dets = detect_tables(a < 200, img.width, img.height)
            L = lbp.get(p, [])
            hs = sorted(l["y1"] - l["y0"] for l in L) or [30]
            if not dets:
                print("p%d: 未检出表格线" % p)
                continue
            for det in dets:
                for rows, cols, title in cells_from_grid(L, det, hs[len(hs) // 2]):
                    print("p%d 框=%s 列%d 行%d 表题=%s" % (p, det["bbox"], len(cols), len(rows), title[:34]))
                    for r in rows[:12]:
                        print("   ", [c[:20] for c in r])
                    print()
        return 0
    out_json = os.path.join(d, "_grid_scan.json")
    res = json.load(io.open(out_json, encoding="utf-8")) if os.path.exists(out_json) else {}
    lo = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    hi = int(sys.argv[4]) if len(sys.argv) > 4 else max(lbp)
    t0 = time.time()
    for p in sorted(lbp):
        if not (lo <= p <= hi) or str(p) in res:
            continue
        img, a = page_lines_and_image(doc, p, tmp)
        L = lbp[p]
        hs = sorted(l["y1"] - l["y0"] for l in L) or [30]
        got = []
        for det in detect_tables(a < 200, img.width, img.height):
            for rows, cols, title in cells_from_grid(L, det, hs[len(hs) // 2]):
                filled = sum(1 for r in rows if sum(1 for c in r if c) >= 2)
                if filled < 2 or len(rows) < 2:
                    continue
                got.append({"bbox": list(det["bbox"]), "cols": [list(c) for c in cols],
                            "rows": rows, "filled": filled, "title": title})
        if got:
            res[str(p)] = got
        if len(res) % 25 == 0:
            json.dump(res, io.open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    json.dump(res, io.open(out_json, "w", encoding="utf-8"), ensure_ascii=False)
    print("累计表格页 %d，本段用时 %.0fs" % (len(res), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
