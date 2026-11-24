# -*- coding: utf-8 -*-
"""清理误检图：重复出现的印章/水印、OCR 切出来的细长碎片、极小的碎块、浅色底图。

规则（2026-11-02 调整，按问题分类 A/B 的实测数据）：
- dup      同一图片在书中出现 ≥5 次 → 水印/印章
- fragment 无图注、宽<300 且高宽比≥4 → 竖排导线/边框碎片
- tiny     面积 < 2500 px² → OCR 碎块，几乎不可能是真插图
- light    面积 ≥2500 且几乎全白（暗像素<1.2%、均值>232）→ 仅登记，浅色线稿也会触发
不再按「小尺寸无编号」批量删除（之前把真插图删掉了，如城市发展史 255→1）。
"""
import hashlib
import json
import os
import argparse
from collections import Counter

from PIL import Image
from figure_cleanup_log import record_removal


def md5(fp):
    with open(fp, "rb") as stream:
        return hashlib.file_digest(stream, "md5").hexdigest()


def clean_figures(book_dir, dry=False, confirmed=()):
    path = os.path.join(book_dir, "book.json")
    with open(path, encoding="utf-8-sig") as stream:
        book = json.load(stream)
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
            with Image.open(fp) as source:
                im = source.convert("L")
            iw, ih = im.size
            area = iw * ih
            cap = (b.get("caption") or "").strip()
            if not cap and iw < 300 and (ih / max(1, iw)) >= 4.0:
                cand[b["id"]] = "fragment"
                continue
            if area < 2500:
                cand[b["id"]] = "tiny"
                continue
            histogram = im.histogram()
            n = sum(histogram)
            mean = sum(value * count for value, count in enumerate(histogram)) / n
            dark = sum(histogram[:170]) / n
            if dark < 0.012 and mean > 232:
                cand[b["id"]] = "light"
        except Exception:
            pass

    if set(confirmed) - cand.keys():
        raise ValueError("指定图块不在当前清图候选中")
    kept, removed = [], []
    reasons = []
    for b in book["blocks"]:
        if b["type"] == "figure" and b.get("id") in cand:
            reason = cand[b["id"]]
            action = "remove" if reason != "light" or b["id"] in confirmed else "review"
            reasons.append({"id": b["id"], "page": b.get("page"), "src": b.get("src"),
                            "reason": reason, "action": action})
            if action == "remove":
                removed.append(b)
                continue
        kept.append(b)
    if dry or not reasons:
        return reasons, sum(b.get("type") == "figure" for b in kept)
    receipt = record_removal(book_dir, "clean_watermark_images", removed, {"reasons": reasons})
    print("清理记录:", receipt)
    if not removed:
        return reasons, sum(b.get("type") == "figure" for b in kept)
    book["blocks"] = kept
    figs2 = [b for b in kept if b["type"] == "figure"]
    paras = [b for b in kept if b["type"] == "paragraph"]
    book["stats"]["figures"] = len(figs2)
    book["stats"]["paired"] = sum(1 for f in figs2 if f.get("caption"))
    book["stats"]["paragraphs"] = len(paras)
    temporary = path + ".cleanfigs.tmp"
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(book, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, path)
    return reasons, len(figs2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book_dir")
    parser.add_argument("--dry", action="store_true", help="只输出清图候选及拟执行动作")
    parser.add_argument("--confirm", action="append", default=[], metavar="FIG_ID",
                        help="指定已人工核对的清图候选图块 ID，可重复传入")
    args = parser.parse_args()
    reasons, kept = clean_figures(args.book_dir, args.dry, args.confirm)
    if args.dry:
        print(json.dumps(reasons, ensure_ascii=False, indent=2))
    print("移出图块:", sum(item["action"] == "remove" for item in reasons),
          "| 待核:", sum(item["action"] == "review" for item in reasons), "| 剩图:", kept,
          "| 明细:", dict(Counter(item["reason"] for item in reasons)))


if __name__ == "__main__":
    main()
