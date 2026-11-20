# -*- coding: utf-8 -*-
"""按卷首目录重建《明日之城》的章节（章标题是美术字，OCR 读不到，只能靠目录页）。"""
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

BOOK = "books/明日之城 1880年以来城市规划与设计的思想史(第四版) (彼得·霍尔) "

# 目录页读到：章号、章名、印刷页码（偏移量稍后用正文小节标题校验）
CHAPTERS = [
    (1, "美好城市的另类景象", 2),
    (2, "梦魇之城", 13),
    (3, "杂道之城", 45),
    (4, "田园城市的解决方案", 83),
    (5, "区域规划的诞生", 141),
    (6, "城市美化运动", 189),
    (7, "柯布西耶式的光辉城市", 220),
    (8, "自建之城", 269),
    (9, "公路之城", 300),
    (10, "规划与学术", 354),
    (11, "上下颠倒的规划", 381),
    (12, "褪色的盛世之城", 408),
    (13, "永远的底层阶级之城", 446),
]

# 正文里能对上的小节标题：(正文标题, 目录印刷页码)
ANCHORS = [
    ("萧条、暴力和动乱的威胁", 21),
    ("欧洲的贫民窟城市", 30),
    ("一个国际化的问题", 42),
    ("实践中的可持续城市主义", 419),
]


def build():
    book_dir = os.path.abspath(BOOK)
    with open(os.path.join(book_dir, "book.json"), encoding="utf-8") as f:
        book = json.load(f)
    blocks = book["blocks"]

    # 1) 用锚点小节求「印刷页码 → PDF 页码」偏移
    offsets = []
    for title, printed in ANCHORS:
        idx = None
        for b in blocks:
            if b.get("type") == "heading" and title in (b.get("text") or ""):
                idx = b.get("page")
                break
        if idx:
            offsets.append(idx - printed)
    if not offsets:
        print("锚点全部没对上，放弃")
        return 1
    offsets.sort()
    off = offsets[len(offsets) // 2]
    print("偏移样本:", offsets, "→ 采用", off)
    if len(set(offsets)) > 1:
        print("警告：偏移不一致，仍按中位数处理")

    # 2) 章节起点 = 印刷页 + 偏移
    starts = [(f"第{n}章 {title}", printed + off) for n, title, printed in CHAPTERS]
    starts.append(("参考文献与索引", 485 + off))
    starts = [(t, p) for t, p in starts if p <= book["stats"]["pages"]]
    print("章节起点页:", starts)

    # 3) 按块切章（结构同 rebuild_toc）
    out = []
    first = starts[0][1]
    preface_end = None
    for i, b in enumerate(blocks):
        if (b.get("page") or 0) >= first:
            preface_end = i
            break
    preface_end = preface_end or 0
    if preface_end > 0:
        chunk = blocks[:preface_end]
        out.append({
            "id": "ch-pre", "title": "前言与序", "startBlock": 0,
            "chars": sum(len(b.get("text") or "") for b in chunk
                         if b.get("type") in ("paragraph", "heading")),
            "figures": sum(1 for b in chunk if b.get("type") == "figure"),
            "blocks": chunk,
        })
    for k, (title, page) in enumerate(starts):
        idx = None
        for i, b in enumerate(blocks):
            if (b.get("page") or 0) >= page:
                idx = i
                break
        if idx is None:
            continue
        end = None
        for nxt in range(k + 1, len(starts)):
            for i, b in enumerate(blocks):
                if (b.get("page") or 0) >= starts[nxt][1] and i > idx:
                    end = i
                    break
            if end is not None:
                break
        end = end if end is not None else len(blocks)
        chunk = blocks[idx:end]
        if not chunk:
            continue
        out.append({
            "id": "ch-%d" % (len(out)),
            "title": title,
            "startBlock": idx,
            "chars": sum(len(b.get("text") or "") for b in chunk
                         if b.get("type") in ("paragraph", "heading")),
            "figures": sum(1 for b in chunk if b.get("type") == "figure"),
            "blocks": chunk,
        })

    print("拟重建章节 %d 个：" % len(out))
    for c in out:
        print("  p%-4s %-24s %d 字 图%d" % (blocks[c["startBlock"]].get("page"), c["title"], c["chars"], c["figures"]))
    if "--dry" in sys.argv:
        return 0
    book["chapters"] = out
    book["stats"]["chapters"] = len(out)
    with open(os.path.join(book_dir, "book.json"), "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    print("已写入", os.path.basename(book_dir))
    return 0


if __name__ == "__main__":
    sys.exit(build())
