# -*- coding: utf-8 -*-
"""处理「文字行被误检成插图」的假图：

- 裁图高度 ≤ 130px 且图内 OCR 出中文（整行文字）→ 把文字还原成段落，删掉假图；
- 裁图内没有任何文字、且很高/很空 → 也删掉。

这样既补回丢失的正文行，也不再把这些行算成「无注图」。

用法：
    python tools/clean_text_fragments.py "books/某本书" [--dry]
"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _btext(b):
    """块文本要算进表格：post 之后 stats.chars 曾凭空少 8800 字（2026-11-05 实测）。"""
    if b.get("type") == "table":
        rows = [" ".join(str(c) for c in (r or [])) for r in (b.get("rows") or [])]
        return ((b.get("caption") or "") + " " + " ".join(rows)).strip()
    return b.get("text") or b.get("caption") or ""


def norm(s):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s or "")


def merge_fragments(book):
    """把还原出来的文字行并回上一段（上一段没有句末标点时）。"""
    blocks = book["blocks"]
    merged = 0
    out = []
    for b in blocks:
        if b.get("fragment") and out:
            prev = out[-1]
            if prev.get("type") == "paragraph" and prev.get("page") == b.get("page"):
                pt = (prev.get("text") or "").rstrip()
                if pt and not pt.endswith(("。", "！", "？", "”", "…")):
                    prev["text"] = pt + (b.get("text") or "")
                    merged += 1
                    continue
        out.append(b)
    dropped = len(blocks) - len(out)
    book["blocks"] = out
    return merged, dropped


def run(book_dir, dry=False):
    import fix_captions
    engine = fix_captions.ocr_engine()
    bj = os.path.join(book_dir, "book.json")
    book = json.load(open(bj, encoding="utf-8"))
    blocks = book["blocks"]

    page_text = {}
    for b in blocks:
        if b.get("type") in ("paragraph", "heading"):
            pg = b.get("page")
            page_text[pg] = page_text.get(pg, "") + norm(b.get("text"))

    targets = [b for b in blocks if b.get("type") == "figure" and not (b.get("caption") or "").strip()]
    recovered = dropped = 0
    for f in targets:
        p = os.path.join(book_dir, f.get("src") or "")
        box = f.get("box") or []
        h = (box[3] - box[1]) if len(box) >= 4 else 0
        try:
            res, _ = engine(str(p)) if os.path.exists(p) else ([], None)
        except Exception:
            continue
        items = [(t[1] or "").strip() for t in (res or [])]
        txt = "".join(items)
        key = norm(txt)
        idx = blocks.index(f)
        if key and h <= 130 and re.search(r"[\u4e00-\u9fff]", txt):
            pgtext = page_text.get(f.get("page"), "")
            if key and key in pgtext:
                # 页面上已经有这段文字，直接删假图
                blocks.remove(f)
                dropped += 1
                continue
            para = {
                "type": "paragraph",
                "text": txt,
                "page": f.get("page"),
                "y": f.get("y"),
                "col": f.get("col", 0),
                "indent": False,
                "fragment": True,
            }
            blocks.insert(idx, para)
            blocks.remove(f)
            recovered += 1
            page_text[f.get("page")] = pgtext + key
        elif not key and h > 300:
            blocks.remove(f)
            dropped += 1
    merged, mdrop = merge_fragments(book)
    if merged:
        print("   文字行并回上一段 %d 处" % merged)
    stats = book.setdefault("stats", {})
    figs_left = [b for b in blocks if b.get("type") == "figure"]
    stats["figures"] = len(figs_left)
    stats["paired"] = sum(1 for b in figs_left if (b.get("caption") or "").strip())
    stats["paragraphs"] = sum(1 for b in blocks if b.get("type") == "paragraph")
    stats["chars"] = sum(len(_btext(b)) for b in blocks if b.get("type") in ("paragraph", "heading", "table"))
    print("   文字行还原 %d 段，删假图 %d 幅；剩图 %d（配注 %d），正文 %d 字"
          % (recovered, dropped, stats["figures"], stats["paired"], stats["chars"]))
    if not dry:
        json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    return recovered, dropped


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    dry = "--dry" in sys.argv
    for d in args:
        print("==", os.path.basename(d)[:40])
        run(d, dry)


if __name__ == "__main__":
    main()
