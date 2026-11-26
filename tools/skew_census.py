#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""倾斜普查 skew_census.py  (2026-11-26)

目的：量化「页级扫描倾斜」在九本公开书（全页）与主库（抽样）里的真实分布，
验证已知乱码修复案例（某竖排书三页）是个例还是系统性问题。

方法：投影方差角扫描（与 vertical_ocr.DESKW 同源原语，粗网格 ±2.5°/0.5°）。
  - 竖排书信号 = 列向墨迹投影方差；横排书信号 = 行向投影方差。
  - ratio = var(最优角)/var(0°)。判斜：|角|>=1.0° 且 ratio>=1.5（任一向命中）。
  - 图版/空白页天然 ratio 噪声大：另记 ink 结构分，低结构页不参与全书统计。
不改动任何书数据，只产出 _工作记录/倾斜普查_20261126/*.json。

用法：
  python skew_census.py png  <pages目录> <输出json> [tag]
  python skew_census.py pdf  <pdf路径>   <输出json> [每N页取1]
"""
import json, math, os, sys, time
import numpy as np
from PIL import Image
from multiprocessing import Pool

LO, HI, STEP = -2.5, 2.5, 0.5
ANGS = [round(a, 2) for a in np.arange(LO, HI + 1e-9, STEP)]

def prof_var(arr, axis):
    # axis=0 -> 列向（对竖排）；axis=1 -> 行向（对横排）
    ink = (arr < 180).sum(axis=axis).astype(np.float64)
    ink[ink >= 0.9 * arr.shape[1 - axis]] = 0
    sens = max(3, int(arr.shape[axis] * 0.004)) if axis == 0 else max(3, int(arr.shape[0] * 0.004))
    return float(ink.var())

def scan_gray(g):
    H = g.shape[0]
    band0 = g[int(H * 0.06):int(H * 0.94)]
    base_c, base_r = prof_var(band0, 0), prof_var(band0, 1)
    out = {}
    for ax, base in (("col", base_c), ("row", base_r)):
        best_a, best_v = 0.0, base
        for a in ANGS:
            if abs(a) < 1e-6:
                continue
            im = Image.fromarray(g)
            gg = np.array(im.rotate(a, resample=Image.BILINEAR, fillcolor=255, expand=False))
            b = gg[int(H * 0.06):int(H * 0.94)]
            v = prof_var(b, 0 if ax == "col" else 1)
            if v > best_v:
                best_a, best_v = a, v
        r = best_v / base if base > 1e-9 else 0.0
        out[ax] = {"a": best_a, "v0": round(base, 1), "v1": round(best_v, 1), "ratio": round(r, 3)}
    inkmean = float((g < 180).mean())
    strong = max(out["col"]["v0"], out["row"]["v0"])
    out["ink"] = round(inkmean, 4)
    out["struct"] = round(strong, 1)
    flag_c = abs(out["col"]["a"]) >= 1.0 and out["col"]["ratio"] >= 1.5
    flag_r = abs(out["row"]["a"]) >= 1.0 and out["row"]["ratio"] >= 1.5
    out["flag"] = bool(flag_c or flag_r)
    return out

def work_png(path):
    try:
        im = Image.open(path).convert("L")
        if im.width > 950:
            im = im.resize((950, int(im.height * 950 / im.width)), Image.BILINEAR)
        return os.path.basename(path), scan_gray(np.array(im))
    except Exception as e:
        return os.path.basename(path), {"error": str(e)[:120]}

def init_pdf(p, sc):
    import fitz
    work_pdf.doc = fitz.open(p)
    work_pdf.scale = sc

def work_pdf(args):
    pdf, pno = args
    try:
        import fitz
        doc = work_pdf.doc
        pg = doc[pno]
        pm = pg.get_pixmap(matrix=fitz.Matrix(work_pdf.scale, work_pdf.scale), colorspace=fitz.csGRAY)
        g = np.frombuffer(pm.samples, dtype=np.uint8).reshape(pm.height, pm.width)
        if g.shape[1] > 950:
            im = Image.fromarray(g).resize((950, int(g.shape[0] * 950 / g.shape[1])), Image.BILINEAR)
            g = np.array(im)
        return "p%03d" % (pno + 1), scan_gray(g)
    except Exception as e:
        return "p%03d" % (pno + 1), {"error": str(e)[:120]}

def main():
    mode, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
    tag = sys.argv[4] if len(sys.argv) > 4 else os.path.basename(src)
    t0 = time.time()
    if mode == "png":
        files = sorted(os.path.join(src, f) for f in os.listdir(src) if f.endswith("_x3.png"))
        with Pool(10) as pool:
            recs = pool.map(work_png, files, chunksize=8)
    else:
        step = int(sys.argv[5]) if len(sys.argv) > 5 else 20
        import fitz
        doc = fitz.open(src)
        pages = list(range(0, doc.page_count, step))
        doc.close()
        # 每 worker 各自开一次 doc
        with Pool(10, initializer=init_pdf, initargs=(src, 1.9)) as pool:
            recs = pool.map(work_pdf, [(src, p) for p in pages], chunksize=4)
    res = {"tag": tag, "mode": mode, "src": src, "pages": len(recs),
           "seconds": round(time.time() - t0, 1), "recs": dict(recs)}
    fl = [k for k, v in res["recs"].items() if v.get("flag")]
    res["flagged"] = fl
    res["flag_rate"] = round(len(fl) / max(1, len(recs)), 4)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False)
    print("%s pages=%d flagged=%d rate=%.3f sec=%.1f" % (tag, len(recs), len(fl), res["flag_rate"], res["seconds"]))

if __name__ == "__main__":
    main()
