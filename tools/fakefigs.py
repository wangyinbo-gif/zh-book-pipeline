# -*- coding: utf-8 -*-
"""删除假插图并保持目录正确：删块 / 插回文字段后，按「旧章起始页」重新对齐章节。"""
import json
import os
import sys


def _btext(b):
    """块文本要算进表格：post 之后 stats.chars 曾凭空少 8800 字（2026-11-05 实测）。"""
    if b.get("type") == "table":
        rows = [" ".join(str(c) for c in (r or [])) for r in (b.get("rows") or [])]
        return ((b.get("caption") or "") + " " + " ".join(rows)).strip()
    return b.get("text") or b.get("caption") or ""


def chapter_start_pages(book):
    blocks = book["blocks"]
    out = []
    for c in book.get("chapters") or []:
        sb = c.get("startBlock") or 0
        page = blocks[sb].get("page") if 0 <= sb < len(blocks) else None
        out.append((c.get("title") or "", page))
    return out


def resync_chapters(book, start_pages):
    """按章节起始页重建 chapters（保持顺序/标题，重算 startBlock、blocks、chars、figures）。"""
    blocks = book["blocks"]
    n = len(blocks)
    pages = [b.get("page") or 0 for b in blocks]
    idxs = []
    prev = 0
    for _title, page in start_pages:
        if page is None:
            idxs.append(prev)
            continue
        i = prev
        while i < n and pages[i] < page:
            i += 1
        idxs.append(min(i, n))
        prev = idxs[-1]
    out = []
    for k, (title, _page) in enumerate(start_pages):
        s = idxs[k]
        e = idxs[k + 1] if k + 1 < len(idxs) else n
        chunk = blocks[s:e]
        out.append({
            "id": "ch-%d" % k,
            "title": title,
            "startBlock": s,
            "chars": sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading")),
            "figures": sum(1 for b in chunk if b.get("type") == "figure"),
            "blocks": chunk,
        })
    book["chapters"] = out
    book["stats"]["chapters"] = len(out)


def apply(book_dir, drop_ids, texts=None):
    """drop_ids: 要删的假图 id；texts: {id: 图内文字} 有文字就并回正文。返回删了几张。"""
    texts = texts or {}
    bj = os.path.join(book_dir, "book.json")
    book = json.load(open(bj, encoding="utf-8"))
    blocks = book["blocks"]
    starts = chapter_start_pages(book)
    n = 0
    for f in list(blocks):
        if f.get("type") != "figure" or f.get("id") not in drop_ids:
            continue
        txt = texts.get(f.get("id")) or ""
        idx = blocks.index(f)
        if txt:
            prev = None
            for j in range(idx - 1, -1, -1):
                if blocks[j].get("type") == "paragraph" and blocks[j].get("page") == f.get("page"):
                    prev = blocks[j]
                    break
            pt = (prev.get("text") or "").rstrip() if prev else ""
            if prev and pt and not pt.endswith(("。", "！", "？", "”", "…")):
                prev["text"] = pt + txt
            else:
                blocks.insert(idx, {"type": "paragraph", "text": txt, "page": f.get("page"),
                                    "y": f.get("y"), "col": f.get("col", 0), "fragment": True})
        blocks.remove(f)
        n += 1
    figs = [b for b in blocks if b.get("type") == "figure"]
    paired = sum(1 for b in figs if (b.get("caption") or "").strip())
    book["stats"]["figures"] = len(figs)
    book["stats"]["paired"] = paired
    book["stats"]["paragraphs"] = sum(1 for b in blocks if b.get("type") == "paragraph")
    book["stats"]["chars"] = sum(len(_btext(b)) for b in blocks if b.get("type") in ("paragraph", "heading", "table"))
    resync_chapters(book, starts)
    json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return n, len(figs), paired
