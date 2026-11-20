# -*- coding: utf-8 -*-
"""用「页眉里的章名」重建目录（适合章节标题页被当成图片/漏识别的书）。

页眉通常印着「第X章 章名 页码」，据此可以准确得到每章从哪一页开始、章名是什么。
"""
import glob
import json
import os
import re
import sys

CH = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?章\s*(.*)$")
NUM = re.compile(r"\d{1,4}$")


def load_pages(book_dir):
    files = (sorted(glob.glob(os.path.join(book_dir, "pages", "page_*_ocr2.json")))
             or sorted(glob.glob(os.path.join(book_dir, "pages", "page_*_ocr.json"))))
    pages = {}
    for f in files:
        m = re.search(r"page_(\d+)_", os.path.basename(f))
        if not m:
            continue
        pages[int(m.group(1))] = json.load(open(f, encoding="utf-8"))
    return pages


def header_chapters(pages):
    """返回 [{num, name, start, end}]，按页码顺序。"""
    found = {}
    for n in sorted(pages):
        lines = pages[n]
        if not lines:
            continue
        h = max(l["y1"] for l in lines)
        for l in lines:
            t = (l.get("text") or "").strip()
            m = CH.match(t)
            if not m or l["y1"] > h * 0.10:
                continue
            name = NUM.sub("", m.group(2)).strip(" 　·-—")
            if len(name) > 40:
                name = name[:40]
            found[n] = (m.group(1), name)
            break
    seq = []
    for n in sorted(found):
        num, name = found[n]
        if seq and seq[-1]["num"] == num:
            seq[-1]["end"] = n
            if len(name) > len(seq[-1]["name"]):
                seq[-1]["name"] = name
        else:
            seq.append({"num": num, "name": name, "start": n, "end": n})
    # 过滤太短的“章”（多半是目录页/附录页误判）
    seq = [c for c in seq if c["end"] - c["start"] >= 4 or len(c["name"]) >= 6]
    return seq


def main():
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    book = json.load(open(path, encoding="utf-8"))
    pages = load_pages(book_dir)
    seq = header_chapters(pages)
    if not seq:
        print("没有从页眉找到章，放弃")
        return
    print("识别到章节：")
    for c in seq:
        print("  第%s章 %s（p%s–%s）" % (c["num"], c["name"], c["start"], c["end"]))

    blocks = book["blocks"]
    inserted_titles = set()
    # 去掉旧的“第X章”标题块，避免重复
    blocks = [b for b in blocks if not (b["type"] == "heading" and CH.match((b.get("text") or "").strip())
                                        and len(CH.match((b.get("text") or "").strip()).group(2)) < 40)]
    # 在每章起始页插入标题块
    titles = inserted_titles
    for c in seq:
        title = "第%s章" % c["num"] + ((" " + c["name"]) if c["name"] else "")
        titles.add(title)
        idx = len(blocks)
        for i, b in enumerate(blocks):
            if (b.get("page") or 0) >= c["start"]:
                idx = i
                break
        blocks.insert(idx, {"type": "heading", "text": title, "page": c["start"], "y": 0})

    # 最后一章之后的剩余部分（附录/索引/参考文献）单独成一章
    last_end = max(c["end"] for c in seq)
    rest = [(i, b) for i, b in enumerate(blocks)
            if b["type"] == "paragraph" and (b.get("page") or 0) > last_end + 1]
    if len(rest) >= 10:
        app = re.compile(r"(附录|索引|参考文献|后记|图片来源|人名|术语|致谢)")
        title = "附录与索引"
        start_page = rest[0][1].get("page")
        idx = rest[0][0]
        for i, b in rest:
            t = (b.get("text") or "").strip()
            if 0 < len(t) <= 20 and app.search(t):
                title = t
                start_page = b.get("page")
                idx = i
                break
        titles.add(title)
        blocks.insert(idx, {"type": "heading", "text": title, "page": start_page, "y": 0})

    # 按插入的标题块重建章节
    heads = [(i, b) for i, b in enumerate(blocks)
             if b["type"] == "heading"
             and (CH.match((b.get("text") or "").strip()) or b.get("text") in inserted_titles)]
    chapters = []
    first = heads[0][0] if heads else len(blocks)
    if blocks[:first]:
        chapters.append({"id": "ch-0", "title": "前言与序", "startBlock": 0,
                         "blocks": blocks[:first]})
    for k, (start, b) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(blocks)
        ch_blocks = blocks[start + 1:end]
        if not ch_blocks:
            continue
        chapters.append({"id": f"ch-{len(chapters)}", "title": b["text"].strip(),
                         "startBlock": start, "blocks": ch_blocks})
    # 丢掉过短的“目录残留”章节（并到上一章）
    merged = []
    for ch in chapters:
        cc = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        if merged and cc < 60 and ch["title"] not in ("前言与序",):
            merged[-1]["blocks"] += ch["blocks"]
            continue
        merged.append(ch)
    chapters = merged

    for ch in chapters:
        ps = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(ps) if ps else None
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b["type"] == "figure")

    book["blocks"] = blocks
    book["chapters"] = chapters
    book["stats"]["chapters"] = len(chapters)
    json.dump(book, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_preview import build_preview
        build_preview(book, os.path.join(book_dir, "preview.html"))
    except Exception as e:
        print("预览重建失败:", e)
    print("新目录：")
    for i, c in enumerate(chapters):
        print("  %2d %-40s chars=%s figs=%s" % (i + 1, c["title"][:38], c["chars"], c["figures"]))


if __name__ == "__main__":
    main()
