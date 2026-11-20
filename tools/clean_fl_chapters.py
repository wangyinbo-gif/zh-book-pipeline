# -*- coding: utf-8 -*-
"""清理某本书的章节：丢掉误识别的「章」（如「第557章」「第37章 和第44章…」），
把被丢弃章的内容并入前一章。"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
Q = os.path.join(ROOT, "inbox", "未达标书稿")
GOOD = re.compile(r"^第\s?(\d{1,3})\s?章\s*([\u4e00-\u9fff][^，。；：、（）()]{1,28})$")


def main():
    dry = "--dry" in sys.argv
    d = [x for x in os.listdir(Q) if os.path.isdir(os.path.join(Q, x)) and "书名关键词" in x][0]
    book = json.load(open(os.path.join(os.path.join(Q, d), "book.json"), encoding="utf-8"))
    blocks = book["blocks"]
    chs = book["chapters"]
    kept, dropped = [], 0
    prev_n = 0
    for c in chs:
        t = (c.get("title") or "").strip()
        if t == "前言与序":
            kept.append(c)
            continue
        m = GOOD.match(t)
        n = int(m.group(1)) if m else 0
        if m and 1 <= n <= 58 and n > prev_n:
            prev_n = n
            kept.append(c)
        else:
            dropped += 1
            if kept:
                kept[-1]["blocks"] = kept[-1].get("blocks", []) + c.get("blocks", [])
                kept[-1]["chars"] = kept[-1].get("chars", 0) + c.get("chars", 0)
                kept[-1]["figures"] = kept[-1].get("figures", 0) + c.get("figures", 0)
    print("原章节 %d → 保留 %d（丢弃 %d）" % (len(chs), len(kept), dropped))
    for c in kept[:6]:
        print("   %-30s %d 字" % (c["title"], c["chars"]))
    print("   ...")
    for c in kept[-3:]:
        print("   %-30s %d 字" % (c["title"], c["chars"]))
    if dry:
        return 0
    book["chapters"] = kept
    book["stats"]["chapters"] = len(kept)
    json.dump(book, open(os.path.join(os.path.join(Q, d), "book.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print("已写入")
    return 0


if __name__ == "__main__":
    sys.exit(main())
