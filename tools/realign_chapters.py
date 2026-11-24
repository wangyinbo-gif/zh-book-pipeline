# -*- coding: utf-8 -*-
"""把每本书的章节边界重新对齐到当前 blocks：
- 用章内首块在 blocks 里定位（id/文字/页一致）；
- 找不到时退回旧起始页；
- 重建 startBlock / endBlock（末块下标，空章为 startBlock - 1）/ blocks / chars / figures。
用法：python tools\realign_chapters.py [book_dir ...]（不给参数=全库）
"""
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile

B = str(Path(__file__).resolve().parents[1] / "books")


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
    with open(bj, encoding="utf-8-sig") as stream:
        book = json.load(stream)
    if not book.get("chapters"):
        return 0
    fallback = realign_book(book)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=book_dir, delete=False) as stream:
        temporary = stream.name
        try:
            json.dump(book, stream, ensure_ascii=False, indent=1)
            stream.close()
            os.replace(temporary, bj)
        finally:
            Path(temporary).unlink(missing_ok=True)
    if verbose:
        print("  %-24s 章%-3d 回退定位 %d" % (os.path.basename(book_dir)[:22], len(book["chapters"]), fallback))
    return fallback


def chapter_chars(chunk):
    """章字数：段落/标题正文 + 表格（caption + 单元格）。表格章的正文在 rows 里，
    只数 paragraph/heading 会把纯表格章算成 0 字，gate「目录结构·空章」会误判。"""
    total = 0
    for b in chunk:
        t = b.get("type")
        if t in ("paragraph", "heading"):
            total += len(b.get("text") or "")
        elif t == "table":
            total += len(b.get("caption") or "")
            for row in b.get("rows") or []:
                total += sum(len(cell or "") for cell in row)
    return total


def realign_book(book):
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
            page = c.get("startPage")
            if page is None and 0 <= old < len(blocks):
                page = blocks[old].get("page")
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
        c["endBlock"] = end - 1
        c["blocks"] = chunk
        c["chars"] = chapter_chars(chunk)
        c["figures"] = sum(1 for b in chunk if b.get("type") == "figure")
    return fallback


def check_bounds(book):
    blocks = book["blocks"]
    chapters = book.get("chapters") or []
    for index, chapter in enumerate(chapters):
        start = chapter["startBlock"]
        expected = chapters[index + 1]["startBlock"] if index + 1 < len(chapters) else len(blocks)
        if not 0 <= start <= expected <= len(blocks) or chapter["endBlock"] != expected - 1:
            raise ValueError(f"章节边界异常：{chapter.get('id') or index + 1}")
        if chapter["blocks"] != blocks[start:expected]:
            raise ValueError(f"章内块与全局书稿不一致：{chapter.get('id') or index + 1}")


def main():
    dirs = sys.argv[1:]
    if not dirs:
        dirs = [os.path.join(B, x) for x in sorted(os.listdir(B))
                if os.path.isdir(os.path.join(B, x)) and os.path.exists(os.path.join(B, x, "book.json"))]
    for d in dirs:
        realign(d, verbose=True)


if __name__ == "__main__":
    main()
