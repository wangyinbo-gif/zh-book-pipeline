# -*- coding: utf-8 -*-
"""清理误检图：重复出现的印章/水印、OCR 切出来的细长碎片、极小的碎块、浅色底图。

规则（2026-11-02 调整，按问题分类 A/B 的实测数据）：
- dup      同一图片在书中出现 ≥5 次 → 水印/印章
- fragment 无图注、宽<300 且高宽比≥4 → 竖排导线/边框碎片
- tiny     面积 < 2500 px² → OCR 碎块，几乎不可能是真插图
- light    面积 ≥2500 且几乎全白（暗像素<1.2%、均值>232）→ 浅色底/空白块
不再按「小尺寸无编号」批量删除（之前把真插图删掉了，如城市发展史 255→1）。
"""
import hashlib
import json
import os
import re
import sys

from PIL import Image


def md5(fp):
    return hashlib.md5(open(fp, "rb").read()).hexdigest()


def main():
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    book = json.load(open(path, encoding="utf-8"))
    figs = [b for b in book["blocks"] if b["type"] == "figure"]
    files = {}
    for f in figs:
        fp = os.path.join(book_dir, (f.get("src") or "").replace("/", os.sep))
        if os.path.isfile(fp):
            files[f["id"]] = (fp, md5(fp))

    counts = {}
    for _, (fp, hh) in files.items():
        counts[hh] = counts.get(hh, 0) + 1
    bad_hash = {hh for hh, c in counts.items() if c >= 5}

    cand = {}
    for b in book["blocks"]:
        if b["type"] != "figure":
            continue
        fp, hh = files.get(b.get("id"), (None, None))
        if hh and hh in bad_hash:
            cand[b["id"]] = "dup"
            continue
        if not fp:
            cand[b["id"]] = "missing"   # 裁剪文件已不存在（历史误删）→ 移出正文，避免留下破图
            continue
        try:
            im = Image.open(fp).convert("L")
            iw, ih = im.size
            area = iw * ih
            cap = (b.get("caption") or "").strip()
            if not cap and iw < 300 and (ih / max(1, iw)) >= 4.0:
                cand[b["id"]] = "fragment"
                continue
            if area < 2500:
                cand[b["id"]] = "tiny"
                continue
            arr = list(im.getdata())
            n = len(arr)
            mean = sum(arr) / n
            dark = sum(1 for v in arr if v < 170) / n
            if dark < 0.012 and mean > 232:
                cand[b["id"]] = "light"
        except Exception:
            pass

    kept, dropped = [], 0
    for b in book["blocks"]:
        if b["type"] == "figure" and b.get("id") in cand:
            dropped += 1
            fp, hh = files.get(b.get("id"), (None, None))
            if fp:
                try:
                    os.remove(fp)
                except OSError:
                    pass
            continue
        kept.append(b)
    book["blocks"] = kept
    figs2 = [b for b in kept if b["type"] == "figure"]
    paras = [b for b in kept if b["type"] == "paragraph"]
    book["stats"]["figures"] = len(figs2)
    book["stats"]["paired"] = sum(1 for f in figs2 if f.get("caption"))
    book["stats"]["paragraphs"] = len(paras)
    json.dump(book, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    from collections import Counter
    why = Counter(cand.values())
    print("清理误检图:", dropped, "| 剩图:", len(figs2), "| 明细:", dict(why))


if __name__ == "__main__":
    main()
