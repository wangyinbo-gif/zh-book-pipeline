# -*- coding: utf-8 -*-
"""按正文里的「第N章」标题重建章节（适合章标题单独成行、但卷首目录被删的书）。

用法：
    python tools/fix_toc_heads.py "books/某本书" [--dry]

输出章节结构与 rebuild_toc / c1_chapters 一致：
    {id, title, startBlock, chars, figures, blocks}
这样阅读器的目录才能点击跳转。
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CHAP_RE = re.compile(r"^第\s?(\d{1,2})\s?章$")


def load(book_dir):
    with open(os.path.join(book_dir, "book.json"), encoding="utf-8") as f:
        return json.load(f)


def toc_pages(blocks):
    per = {}
    for b in blocks:
        if b.get("type") not in ("paragraph", "heading"):
            continue
        m = CHAP_RE.match((b.get("text") or "").strip())
        if m:
            per.setdefault(b.get("page") or 0, set()).add(int(m.group(1)))
    return {pg for pg, ns in per.items() if len(ns) >= 3}


def toc_titles(blocks):
    titles = {}
    toc = toc_pages(blocks)
    seq = [b for b in blocks if b.get("type") in ("paragraph", "heading")
           and (b.get("page") or 0) in toc]
    for i, b in enumerate(seq):
        t = (b.get("text") or "").strip()
        if not CHAP_RE.match(t):
            continue
        n = int(CHAP_RE.match(t).group(1))
        if n in titles:
            continue
        for j in range(i + 1, min(i + 3, len(seq))):
            nt = re.sub(r"[.．·…\s]*\d+$", "", (seq[j].get("text") or "").strip()).strip()
            if not nt or CHAP_RE.match(nt) or re.match(r"^[\d.]+$", nt):
                continue
            if re.search(r"[\u4e00-\u9fff]", nt) and 2 <= len(nt) <= 30 \
                    and not nt.startswith(("第", "图", "表")):
                titles[n] = nt
                break
    return titles


def make_chapters(blocks, starts):
    """starts: [(title, page)]，按页切成章节块（结构与 c1_chapters.rebuild 一致）。"""
    out = []
    for k, (title, page) in enumerate(starts):
        idx = None
        for i, blk in enumerate(blocks):
            if (blk.get("page") or 0) >= page:
                idx = i
                break
        if idx is None:
            continue
        if out and idx <= out[-1]["startBlock"]:
            continue
        end = len(blocks)
        for nxt in range(k + 1, len(starts)):
            for i, blk in enumerate(blocks):
                if (blk.get("page") or 0) >= starts[nxt][1] and i > idx:
                    end = i
                    break
            if end != len(blocks):
                break
        chunk = blocks[idx:end]
        out.append({
            "id": "ch-%d" % k,
            "title": title,
            "startBlock": idx,
            "chars": sum(len(b.get("text") or "") for b in chunk
                         if b.get("type") in ("paragraph", "heading")),
            "figures": sum(1 for b in chunk if b.get("type") == "figure"),
            "blocks": chunk,
        })
    return out


def rebuild(book_dir, dry=False):
    book = load(book_dir)
    blocks = book.get("blocks") or []
    heads = [(b.get("page") or 0, (b.get("text") or "").strip())
             for b in blocks if b.get("type") == "heading"]
    cand = [(pg, int(CHAP_RE.match(t).group(1))) for pg, t in heads if CHAP_RE.match(t)]
    if not cand:
        print("  找不到「第N章」标题，放弃")
        return 0
    page_chars = {}
    for b in blocks:
        if b.get("type") == "paragraph":
            pg = b.get("page") or 0
            page_chars[pg] = page_chars.get(pg, 0) + len(b.get("text") or "")
    toc = toc_pages(blocks)
    first_pages = [pg for pg, n in cand if n == 1 and page_chars.get(pg, 0) >= 800]
    if first_pages:
        body_start = min(first_pages)
    else:
        sec = [b.get("page") or 0 for b in blocks
               if b.get("type") == "heading" and re.match(r"^1\.1", (b.get("text") or "").strip())]
        body_start = (min(sec) - 1) if sec else (max(toc) + 1 if toc else 1)
    starts = {}
    for pg, n in cand:
        if pg < body_start:
            continue
        if n not in starts or pg < starts[n]:
            starts[n] = pg
    if len(starts) < 2:
        print("  可识别章数不足 2，放弃")
        return 0
    titles = toc_titles(blocks)
    nums = sorted(starts)
    items = []
    for n in nums:
        t = "第%d章" % n
        if titles.get(n):
            t += " " + titles[n]
        items.append((t, starts[n]))

    chapters = []
    if starts[nums[0]] > 1:
        first_idx = None
        for i, blk in enumerate(blocks):
            if (blk.get("page") or 0) >= starts[nums[0]]:
                first_idx = i
                break
        first_idx = first_idx or 0
        chunk = blocks[:first_idx]
        if chunk:
            chapters.append({
                "id": "ch-pre", "title": "前言与序", "startBlock": 0,
                "chars": sum(len(b.get("text") or "") for b in chunk
                             if b.get("type") in ("paragraph", "heading")),
                "figures": sum(1 for b in chunk if b.get("type") == "figure"),
                "blocks": chunk,
            })
    chapters.extend(make_chapters(blocks, items))
    print("  拟重建章节 %d 个：" % len(chapters))
    for c in chapters:
        print("    startBlock %-6s %-22s %d 字 图%d" % (c["startBlock"], c["title"], c["chars"], c["figures"]))
    if dry:
        return len(chapters)
    book["chapters"] = chapters
    st = book.setdefault("stats", {})
    st["chapters"] = len(chapters)
    st["chars"] = sum(c["chars"] for c in chapters)
    with open(os.path.join(book_dir, "book.json"), "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    return len(chapters)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("book_dir")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    n = rebuild(args.book_dir, args.dry)
    print(("预演：" if args.dry else "完成：") + "章节 %d" % n)


if __name__ == "__main__":
    main()
