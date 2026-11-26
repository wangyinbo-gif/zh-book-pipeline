# -*- coding: utf-8 -*-
"""筑学 · 书稿包后处理：
- 清掉目录页（OCR 会把目录条目当成正文）
- 修正书名（去掉下载站尾标之类后缀）
- 重新按「第N篇/第N章」建章节（只认真实章节，跳过目录里的条目）
- 去掉封面页的假插图，重新连续编号并同步正文引用
"""
import re
import json
import os
import sys

CHAP_RE = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?([篇章])")


def clean_title(name):
    if not name:
        return name
    name = re.sub(r"[\(（]\s*z-library.*?[\)）]", "", name, flags=re.I)
    name = re.sub(r"[\(（]\s*(1lib|z-lib).*?[\)）]", "", name, flags=re.I)
    name = name.replace("(", "（").replace(")", "）")
    name = re.sub(r"\s+", " ", name).strip(" -_·.")
    name = name.replace(" （", "（").replace("） ", "）")
    return name or name


def detect_toc_pages(blocks):
    """目录页特征：短行/纯数字为主，或者一页里塞了很多「第N章」标题。"""
    by_page = {}
    heads = {}
    chars = {}
    for b in blocks:
        pg = b.get("page")
        if pg is None:
            continue
        t = b.get("text") or ""
        if b["type"] == "paragraph":
            by_page.setdefault(pg, []).append(t)
            chars[pg] = chars.get(pg, 0) + len(t)
        elif b["type"] == "heading":
            chars[pg] = chars.get(pg, 0) + len(t)
            if CHAP_RE.match(t.strip()):
                heads[pg] = heads.get(pg, 0) + 1
    toc = []
    for pg, texts in by_page.items():
        if len(texts) < 8:
            continue
        short = 0
        for t in texts:
            t = t.strip()
            if len(t) <= 22 or re.fullmatch(r"[\d\s\.\-–—…·]+", t):
                short += 1
        if short / len(texts) >= 0.6:
            toc.append(pg)
    for pg, n in heads.items():
        if n >= 3 and chars.get(pg, 0) < 900:
            toc.append(pg)
    if not toc:
        return set()
    # 只保留连续的一段（避免把正文里偶尔的短行页误判）
    toc.sort()
    groups = [[toc[0]]]
    for pg in toc[1:]:
        if pg - groups[-1][-1] <= 2:
            groups[-1].append(pg)
        else:
            groups.append([pg])
    best = max(groups, key=len)
    return set(best) if len(best) >= 2 else set()


def fix_book(book):
    blocks = book.get("blocks") or []
    toc_pages = detect_toc_pages(blocks)
    keep = [b for b in blocks if b.get("page") not in toc_pages]
    # 封面/版权页的「图」多半是扫描章，去掉
    keep = [b for b in keep if not (b.get("type") == "figure" and (b.get("page") or 0) <= 2)]

    # ---- 插图重新连续编号 ----
    figs = [b for b in keep if b.get("type") == "figure"]
    old_to_new = {}
    for i, f in enumerate(figs, start=1):
        old = f.get("number")
        if isinstance(old, int):
            old_to_new[old] = i
        f["number"] = i
        f["displayCaption"] = re.sub(r"^图\s*\d+", f"图{i}", f.get("displayCaption") or "").strip()
    if old_to_new:
        def repl(m):
            n = int(m.group(1))
            return f"图{old_to_new.get(n, n)}"
        for b in keep:
            if b.get("type") == "paragraph":
                b["text"] = re.sub(r"图\s*(\d+)", repl, b["text"])

    # ---- 真实章节：目录页之后的「第N篇 / 第N章」标题 ----
    toc_end = max(toc_pages) if toc_pages else 0
    heads = [i for i, b in enumerate(keep)
             if b.get("type") == "heading" and CHAP_RE.match(b.get("text") or "")
             and (b.get("page") or 0) > toc_end]
    chapters = []
    first_head = heads[0] if heads else len(keep)
    front = keep[:first_head]
    if front:
        chapters.append({"id": "ch-0", "title": "前言与序", "startBlock": 0, "blocks": front})
    for k, start in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(keep)
        title = keep[start]["text"].strip()
        ch_blocks = keep[start + 1:end]
        if not ch_blocks:
            continue
        chapters.append({"id": f"ch-{len(chapters)}", "title": title, "startBlock": start, "blocks": ch_blocks})

    for ch in chapters:
        pages = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(pages) if pages else None
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"] if b.get("type") == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b.get("type") == "figure")

    title = clean_title(book.get("bookTitle") or book.get("title") or "")
    # 去掉坏掉的章节（内容过短且不是篇/章标题的漏网条目）
    chapters = [c for c in chapters if c["chars"] >= 80 or c["title"].startswith(("前言", "第"))]

    book["chapters"] = chapters
    book["blocks"] = keep
    book["title"] = title
    book["bookTitle"] = title
    chars = sum(len(b.get("text") or "") for b in keep if b.get("type") == "paragraph")
    fig_list = [b for b in keep if b.get("type") == "figure"]
    book["stats"] = {
        "pages": book.get("stats", {}).get("pages", 0),
        "chars": chars,
        "paragraphs": sum(1 for b in keep if b.get("type") == "paragraph"),
        "figures": len(fig_list),
        "paired": sum(1 for f in fig_list if f.get("caption")),
        "chapters": len(chapters),
    }
    book["_fix"] = {"removedTocPages": sorted(toc_pages), "chapters": len(chapters)}
    return book


def main():
    if len(sys.argv) < 2:
        print("用法: python bookfix.py <book.json>")
        return
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        book = json.load(f)
    book = fix_book(book)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    # 同时重建预览
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_preview import build_preview
        build_preview(book, os.path.join(os.path.dirname(path), "preview.html"))
    except Exception as e:
        print("预览重建失败:", e)
    print("完成:", book["title"], "| 章节", len(book["chapters"]), "| 去掉目录页", book["_fix"]["removedTocPages"])


if __name__ == "__main__":
    main()
