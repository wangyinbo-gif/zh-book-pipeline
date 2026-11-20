# -*- coding: utf-8 -*-
"""筑学 · 目录重组：把「第N篇」并入它下面的「第N章」，目录只保留章。

篇标题、篇导语仍保留在正文里（作为章节开头的标题块），但不再单独成为目录项。
"""
import json
import os
import re
import sys
import glob

ZHANG = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?章")
PIAN = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?篇")


def main():
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    with open(path, encoding="utf-8") as f:
        book = json.load(f)
    blocks = book["blocks"]

    # 对话体：标题常被 OCR 合并进段落（如「JN对话1JB：我们不会…」），先拆出来
    DIALOG = re.compile(r"^[A-Z]{0,3}\s*(对话\s?[0-9一二三四五六七八九十]+)\s*[：:]?")
    split_blocks = []
    for b in blocks:
        if b["type"] == "paragraph":
            t = (b.get("text") or "")
            m = DIALOG.match(t.strip())
            if m and len(t.strip()) > len(m.group(1)) + 4:
                split_blocks.append({"type": "heading", "text": m.group(1), "page": b.get("page"), "y": b.get("y")})
                rest = t.strip()[m.end():].strip()
                if rest:
                    nb = dict(b)
                    nb["text"] = rest
                    split_blocks.append(nb)
                continue
        split_blocks.append(b)
    blocks = split_blocks

    def pick_chapters(bs):
        """按优先级选章标题：第N章 → 第N节 → 第N篇 → 「1 标题」数字式。"""
        zh = [i for i, b in enumerate(bs) if b["type"] == "heading" and ZHANG.match(b.get("text") or "")]
        if len(zh) >= 3:
            return zh, ZHANG
        # 「第N讲」「第N卷」也当作章级
        for word in ("讲", "卷"):
            pat = re.compile(r"^第\s?[0-9一二三四五六七八九十百零两]+\s?" + word)
            hits = [i for i, b in enumerate(bs) if b["type"] == "heading" and pat.match(b.get("text") or "")]
            if len(hits) >= 3:
                return hits, pat
        jie = re.compile(r"^第\s?[0-9一二三四五六七八九十百]+\s?节")
        jie_h = [i for i, b in enumerate(bs) if b["type"] == "heading" and jie.match(b.get("text") or "")]
        if len(jie_h) >= 3:
            return jie_h, jie
        # 篇：优先用正文里的分隔页（整行只有「第N篇」），避免把印刷目录里的
        # 「第N篇 建筑热工学」当成正文标题（会导致目录页被当章、正文篇被去重删掉）
        pian_exact = re.compile(r"^第\s?[0-9一二三四五六七八九十百零两]+\s?篇\s*$")
        pian_h = [i for i, b in enumerate(bs) if b["type"] == "heading" and pian_exact.match(b.get("text") or "")]
        if len(pian_h) < 2:
            pian_h = [i for i, b in enumerate(bs) if b["type"] == "heading" and PIAN.match(b.get("text") or "")]
        if len(pian_h) >= 2:
            return pian_h, PIAN
        # 对话体：对话1 / 对话2 ……
        dlg = re.compile(r"^(对话\s?[0-9一二三四五六七八九十]+)")
        dlg_h = [i for i, b in enumerate(bs) if b["type"] == "heading" and dlg.match((b.get("text") or "").strip())]
        if len(dlg_h) >= 2:
            return dlg_h, dlg
        # 「2 由室与架构形成的住宅构成」这类：数字 + 空格 + 标题
        numt = re.compile(r"^(\d{1,2})\s+\S{2,}")
        cand = []
        for i, b in enumerate(bs):
            if b["type"] != "heading":
                continue
            m = numt.match((b.get("text") or "").strip())
            if m and len((b.get("text") or "").strip()) <= 30:
                cand.append((int(m.group(1)), i))
        if len(cand) >= 3:
            nums = [n for n, _ in cand]
            if len(set(nums)) == len(nums) and min(nums) >= 1:
                return [i for _, i in cand], numt
        return zh, ZHANG

    zhangs = [i for i, b in enumerate(blocks)
              if b["type"] == "heading" and ZHANG.match(b.get("text") or "")]
    pians = [i for i, b in enumerate(blocks)
             if b["type"] == "heading" and PIAN.match(b.get("text") or "")]

    zhangs, MARK = pick_chapters(blocks)

    # 每个章标题：向上找最近的篇标题，若中间没有别的章，就从这个篇开始算本章
    # 先去掉“目录页”：一页上出现 5 个以上章标题，多半是印刷目录，整页不要
    from collections import Counter as _Counter
    page_ch = _Counter(b.get("page") for b in blocks
                       if b["type"] == "heading" and MARK.match(b.get("text") or ""))
    page_para = _Counter(b.get("page") for b in blocks if b["type"] == "paragraph")
    page_chars = _Counter()
    for b in blocks:
        if b["type"] in ("paragraph", "heading"):
            page_chars[b.get("page")] += len(b.get("text") or "")
    # 第一处“章标题 + 足够正文”的页之前，带章标题的页都视为印刷目录
    real_pages = [p for p in page_ch if page_chars.get(p, 0) >= 800 and page_para.get(p, 0) >= 3]
    real_start = min(real_pages) if real_pages else None
    # 只把「真正的印刷目录页」删掉：一页 5 个以上标题，或位于首个正文页之前；
    # 不再按「文字少」删页（篇/章 分隔页往往只有标题，删了会把章节结构打没）
    toc_pages = {p for p, c in page_ch.items()
                 if c >= 5 or (real_start is not None and p < real_start)}
    if toc_pages:
        blocks = [b for b in blocks if b.get("page") not in toc_pages]
        zhangs, MARK = pick_chapters(blocks)
        pians = [i for i, b in enumerate(blocks)
                 if b["type"] == "heading" and PIAN.match(b.get("text") or "")]

    # 去掉重复的章标题块（同章号只保留第一次），并保留最长标题
    def chap_key(t):
        m = MARK.match(t.strip())
        return m.group(1) if m else None

    seen = {}
    new_blocks = []
    for b in blocks:
        if b["type"] == "heading" and chap_key(b["text"]):
            k = chap_key(b["text"])
            if k in seen:
                first = seen[k]
                if len(b["text"].strip()) > len(first["text"].strip()):
                    first["text"] = b["text"].strip()
                continue
            seen[k] = b
        new_blocks.append(b)
    if len(new_blocks) != len(blocks):
        blocks = new_blocks
        zhangs = [i for i, b in enumerate(blocks)
                  if b["type"] == "heading" and MARK.match(b.get("text") or "")]
        pians = [i for i, b in enumerate(blocks)
                 if b["type"] == "heading" and PIAN.match(b.get("text") or "")]

    if MARK is ZHANG:
        # 章级目录：章可以属于前面的「篇」，章起点上提到篇标题
        starts = []
        for z in zhangs:
            start = z
            for p in reversed(pians):
                if p < z:
                    if not any(z2 < z and z2 > p for z2 in zhangs):
                        start = p
                    break
            starts.append((start, z))
        starts.sort()
    else:
        # 篇/节/数字式本身就是章：每章从自己的标题开始
        starts = [(z, z) for z in zhangs]

    chapters = []
    first = starts[0][0] if starts else len(blocks)
    if blocks[:first]:
        chapters.append({"id": "ch-0", "title": "前言与序", "startBlock": 0, "blocks": blocks[:first]})
    for k, (start, z) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(blocks)
        ch_blocks = blocks[start:end]
        if not ch_blocks:
            continue
        chapters.append({
            "id": f"ch-{len(chapters)}",
            "title": blocks[z]["text"].strip(),
            "startBlock": start,
            "blocks": ch_blocks,
        })

    # 合并空章（只有标题没正文），避免目录里出现 0 字章节
    cleaned = []
    for i, ch in enumerate(chapters):
        ch_chars = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        if ch_chars < 60 and i + 1 < len(chapters):
            nxt = chapters[i + 1]
            nxt["blocks"] = ch["blocks"] + nxt["blocks"]
            nxt["startBlock"] = ch["startBlock"]
            continue
        cleaned.append(ch)
    chapters = cleaned

    # 仍然过短的章节（多为章首页残留）并到上一章
    second = []
    for ch in chapters:
        ch_chars = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        if second and ch_chars < 300:
            second[-1]["blocks"] += ch["blocks"]
            continue
        second.append(ch)
    chapters = second

    # 纯“第N节 / 第N章”的标题：用正文第一行短标题补上副标题
    for ch in chapters:
        if re.fullmatch(r"第\s?[0-9一二三四五六七八九十百零两]+\s?[节章篇]", ch["title"]):
            for b in ch["blocks"]:
                if b["type"] in ("heading", "paragraph"):
                    t = (b.get("text") or "").strip()
                    if (2 <= len(t) <= 24 and not re.search(r"[。！？，；：、（(]", t)
                            and not t.startswith("图") and t != ch["title"]
                            and not re.fullmatch(r"第\s?[0-9一二三四五六七八九十百零两]+\s?[节章篇]", t)):
                        ch["title"] = ch["title"] + " " + t
                        break

    for ch in chapters:
        t = ch["title"]
        t = re.sub(r"^(第\s?[0-9一二三四五六七八九十百零两]+\s?章)\s*[，、,。．.；;：:\s]+", r"\1 ", t)
        t = re.sub(r"^(第\s?[0-9一二三四五六七八九十百零两]+\s?章)\s*人章", r"\1 ", t)
        t = re.sub(r"若干千问题", "若干问题", t)
        ch["title"] = t.strip()

    for ch in chapters:
        pages = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(pages) if pages else None
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b["type"] == "figure")

    book["chapters"] = chapters
    book["stats"]["chapters"] = len(chapters)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_preview import build_preview
        build_preview(book, os.path.join(book_dir, "preview.html"))
    except Exception as e:
        print("预览重建失败:", e)
    for i, c in enumerate(chapters):
        print("  %2d %-30s chars=%-6s figs=%s" % (i + 1, c["title"][:28], c["chars"], c["figures"]))
    print("章节:", len(chapters))


if __name__ == "__main__":
    main()
