# -*- coding: utf-8 -*-
"""把每本书的章节边界重新对齐到当前 blocks：
- 用章内首块在 blocks 里定位（id/文字/页一致）；
- 找不到时退回旧起始页；
- 重建 startBlock / blocks / chars / figures。
用法：python tools\realign_chapters.py [book_dir ...]（不给参数=全库）
"""
from paths import BOOKS
import json
import os
import sys

B = BOOKS


def key(b):
    return (b.get("id"), b.get("type"), (b.get("text") or ""), b.get("page"))


def find_from(blocks, target, start):
    tk = key(target)
    for i in range(start, len(blocks)):
        if key(blocks[i]) == tk:
            return i
    # 退化：只比文字+页
    tn = (target.get("type"), (target.get("text") or ""), target.get("page"))
    for i in range(start, len(blocks)):
        if (blocks[i].get("type"), (blocks[i].get("text") or ""), blocks[i].get("page")) == tn:
            return i
    return -1


def realign(book_dir, verbose=False):
    bj = os.path.join(book_dir, "book.json")
    book = json.load(open(bj, encoding="utf-8"))
    blocks = book.get("blocks") or []
    chapters = book.get("chapters") or []
    if not chapters:
        return 0
    starts = []
    cursor = 0
    fallback = 0
    for c in chapters:
        cb = c.get("blocks") or []
        sb = -1
        if cb:
            sb = find_from(blocks, cb[0], cursor)
        if sb < 0:
            # 退回：旧 startBlock 的页
            old = c.get("startBlock") or 0
            page = blocks[old].get("page") if 0 <= old < len(blocks) else None
            if page is not None:
                sb = next((i for i in range(cursor, len(blocks)) if (blocks[i].get("page") or 0) >= page), -1)
            if sb < 0:
                sb = cursor
            fallback += 1
        starts.append(sb)
        cursor = sb
    for k, c in enumerate(chapters):
        sb = starts[k]
        end = starts[k + 1] if k + 1 < len(starts) else len(blocks)
        chunk = blocks[sb:end]
        c["startBlock"] = sb
        c["blocks"] = chunk
        c["chars"] = sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading"))
        c["figures"] = sum(1 for b in chunk if b.get("type") == "figure")
    book["chapters"] = chapters
    json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    if verbose:
        print("  %-24s 章%-3d 回退定位 %d" % (os.path.basename(book_dir)[:22], len(chapters), fallback))
    return fallback


def main():
    dirs = sys.argv[1:]
    if not dirs:
        dirs = [os.path.join(B, x) for x in sorted(os.listdir(B))
                if os.path.isdir(os.path.join(B, x)) and os.path.exists(os.path.join(B, x, "book.json"))]
    for d in dirs:
        realign(d, verbose=True)


if __name__ == "__main__":
    main()
