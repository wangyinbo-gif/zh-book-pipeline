# -*- coding: utf-8 -*-
"""给 book.json 里每幅图补上宽高（阅读器排版要用它预留位置，否则图注会被挤出页面）。"""
import json
import os
import sys

from PIL import Image


def main():
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    with open(path, encoding="utf-8") as f:
        book = json.load(f)
    n = 0
    for b in book.get("blocks", []):
        if b.get("type") != "figure":
            continue
        src = b.get("src") or ""
        if not src:
            continue
        p = os.path.join(book_dir, src.replace("/", os.sep))
        try:
            with Image.open(p) as im:
                b["w"], b["h"] = im.size
                n += 1
        except Exception:
            pass
    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    print("补图尺寸:", n)


if __name__ == "__main__":
    main()
