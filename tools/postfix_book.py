# -*- coding: utf-8 -*-
"""筑学 · 书稿后处理（分句修补）

在已有 book.json 上做三件事，不再重新 OCR：
1. 把「上一段没有句末标点、下一段以 的/了/着/（/小写字母 开头」的续段并回去；
2. 把图例行（如「1一恺撒广场；2一穿堂广场…」）并入最近插图的图注；
3. 把游离在图旁的短标签（≤10 字、无标点）并入最近插图的图注；
4. 按「第N篇/章」重建章节与统计，并重建翻页预览。
"""
import json
import os
import re
import sys
import glob

CHAP_RE = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?([篇章])")
TERM = "。！？…：；”』」)"
CONT_RE = re.compile(r"^(的|地|得|了|着|过|们|[（(]|[a-z0-9])")
LEGEND_RE = re.compile(r"^\d+\s?一\s?[\u4e00-\u9fa5]")


def _btext(b):
    """块文本要算进表格：post 之后 stats.chars 曾凭空少 8800 字（2026-11-05 实测）。"""
    if b.get("type") == "table":
        rows = [" ".join(str(c) for c in (r or [])) for r in (b.get("rows") or [])]
        return ((b.get("caption") or "") + " " + " ".join(rows)).strip()
    return b.get("text") or b.get("caption") or ""


def clean_text(t):
    return (t or "").strip()


def merge_continuations(blocks):
    """把明显是续段的段落并回上一段。"""
    merged = 0
    out = []
    for b in blocks:
        if b["type"] == "paragraph" and out:
            prev = out[-1]
            if prev["type"] == "paragraph":
                pt = clean_text(prev.get("text"))
                ct = clean_text(b.get("text"))
                if pt and ct and pt[-1] not in TERM and CONT_RE.match(ct):
                    prev["text"] = pt + ct
                    pages = set(prev.get("pages") or [prev.get("page")])
                    pages.update(b.get("pages") or [b.get("page")])
                    prev["pages"] = sorted(p for p in pages if p)
                    merged += 1
                    continue
        out.append(b)
    return out, merged


def fig_near(block, figs):
    """同一页上、y 距离最近的插图。"""
    pg = block.get("page")
    cands = [f for f in figs if f.get("page") == pg]
    if not cands:
        return None
    y = block.get("y") or 0
    return min(cands, key=lambda f: abs((f.get("y") or 0) - y))


def absorb_labels(blocks):
    """把图例/图旁短标签并进图注。"""
    figs = [b for b in blocks if b["type"] == "figure"]
    moved = 0
    out = []
    for b in blocks:
        if b["type"] == "paragraph":
            t = clean_text(b.get("text"))
            is_legend = bool(LEGEND_RE.match(t)) and len(t) <= 80 and "；" in t
            is_label = (len(t) <= 10 and not re.search(r"[。！？，；：、]$", t)
                        and (b.get("page") or 0) >= 22
                        and not re.match(r"^\d{4}\s?年", t))
            if (is_legend or is_label) and figs:
                f = fig_near(b, figs)
                near_y = f is not None and abs((f.get("y") or 0) - (b.get("y") or 0)) < 450
                if f is not None and near_y:
                    f["caption"] = ((f.get("caption") or "") + " " + t).strip()
                    moved += 1
                    continue
        out.append(b)
    return out, moved


def rebuild_chapters(blocks):
    heads = [i for i, b in enumerate(blocks)
             if b["type"] == "heading" and CHAP_RE.match(b.get("text") or "")]
    chapters = []
    first = heads[0] if heads else len(blocks)
    front = blocks[:first]
    if front:
        chapters.append({"id": "ch-0", "title": "前言与序", "startBlock": 0, "blocks": front})
    for k, start in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(blocks)
        ch_blocks = blocks[start + 1:end]
        if not ch_blocks:
            continue
        chapters.append({"id": f"ch-{len(chapters)}", "title": blocks[start]["text"].strip(),
                         "startBlock": start, "blocks": ch_blocks})
    for ch in chapters:
        pages = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(pages) if pages else None
        ch["chars"] = sum(len(_btext(b)) for b in ch["blocks"] if b["type"] in ("paragraph", "heading", "table"))
        ch["figures"] = sum(1 for b in ch["blocks"] if b["type"] == "figure")
    return chapters


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/postfix_book.py <book目录>")
        return
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    with open(path, encoding="utf-8") as f:
        book = json.load(f)
    blocks = book["blocks"]

    blocks, m1 = merge_continuations(blocks)
    blocks, m2 = absorb_labels(blocks)
    chapters = rebuild_chapters(blocks)

    book["blocks"] = blocks
    book["chapters"] = chapters
    figs = [b for b in blocks if b["type"] == "figure"]
    paras = [b for b in blocks if b["type"] == "paragraph"]
    book["stats"] = {
        "pages": book.get("stats", {}).get("pages", 0),
        "chars": sum(len(_btext(b)) for b in paras),
        "paragraphs": len(paras),
        "figures": len(figs),
        "paired": sum(1 for f in figs if f.get("caption")),
        "chapters": len(chapters),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_preview import build_preview
        build_preview(book, os.path.join(book_dir, "preview.html"))
    except Exception as e:
        print("预览重建失败:", e)
    print("合并续段:", m1, "| 图例/标签并入图注:", m2)
    print("章节:", len(chapters), "| 段落:", len(paras), "| 字数:", book["stats"]["chars"], "| 图:", len(figs))


if __name__ == "__main__":
    main()
