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

# ---- 目录判据：反目录证据（内容表 / 术语表否决）词表与正则 ----
# 真目录特征：条目名短 + 点线引导符 + 行尾页码；正文制度表/术语表（如营造法式「制度」
# 「腰串：每间广一尺,即长四寸六分。」）特征：度量/工艺词、长句、行末句号、无行尾页码。
# 保守方向：宁可漏删真目录（尚有人工兜底），不可误删正文（静默丢失）。
_TOC_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
# 点线引导符：≥4 个连续分隔/空白字符（⋯ … · 冒号串 点串 破折号串 等）
_TOC_LEADER_RE = re.compile(r"[：:，,。.．…·、\-–—_\s]{4,}")
_TOC_NUM_RE = re.compile(r"[\d\s\.\-–—…·]+")
# 行尾页码：阿拉伯/全角数字（可带括号），或古籍中文数字页码
_TOC_PAGEEND_RE = re.compile(r"[\(（]?[０-９\d]{1,5}[\)）]?\s*$")
_TOC_CJKPAGEEND_RE = re.compile(r"[〇零一二三四五六七八九十百千两]{1,6}\s*$")
_TOC_SENT_RE = re.compile(r"[。！？!?；;]")
# 度量/工艺词：仅取强指向「规限制法」的维度词、单位与专词；
# 泛字（分/步/间/柱/版/作/造）易误伤古籍条目名，故不计入否决触发。
_TOC_CRAFT_WORDS = ("长", "長", "广", "廣", "高", "厚", "寸", "尺", "丈", "厘", "釐",
                    "随", "隨", "视", "視", "同上", "之制", "之製", "积而为法", "積而為法", "定法")
_TOC_SHORT_MAXLEN = 22      # 短行的字符上限（含标点）
_TOC_SHORT_RATIO = 0.6      # 短行率达标线（沿用原判据）
_TOC_CRAFT_RATIO = 0.25     # (a) 度量/工艺词行占比 ≥25% → 反目录
_TOC_LONG_MINLEN = 20       # (b) 长行判定的 CJK 字数下限
_TOC_LONG_RATIO = 0.15      # (b) ≥20 字长行占比 ≥15% 且无页码/点线证据 → 反目录
_TOC_PROSE_MINLEN = 12      # (c) 散文句式行的 CJK 字数下限
_TOC_PROSE_RATIO = 0.15     # (c) 句末标点的长句行占比 ≥15% 且无页码/点线证据 → 反目录
_TOC_MARK_RATIO = 0.30      # 页码/点线证据行占比；≥此值视为具备目录特征，不施 (b)(c) 否决


def clean_title(name):
    if not name:
        return name
    name = re.sub(r"[\(（]\s*z-library.*?[\)）]", "", name, flags=re.I)
    name = re.sub(r"[\(（]\s*(1lib|z-lib).*?[\)）]", "", name, flags=re.I)
    name = name.replace("(", "（").replace(")", "）")
    name = re.sub(r"\s+", " ", name).strip(" -_·.")
    name = name.replace(" （", "（").replace("） ", "）")
    return name or name


def _page_toc_evidence(texts):
    """统计一页文本行的目录 / 反目录证据指标，供 detect_toc_pages 否决使用。

    返回各项占比：
    - short:   短行率（≤22 字 / 纯数字符号 / 含点线引导符 → 视为目录式短行）
    - long:    CJK 字数 ≥20 的长行占比（目录页几乎无长行）
    - craft:   含度量/工艺词的行的占比（制度表 / 术语表强特征）
    - tocmark: 含点线引导符或行尾页码的行的占比（真目录的正面证据）
    - prose:   句末标点 + CJK≥12 的叙述性长行占比（正文段落强特征）
    """
    n = len(texts)
    if n == 0:
        return {"n": 0, "short": 0.0, "long": 0.0, "craft": 0.0, "tocmark": 0.0, "prose": 0.0}
    short = longn = craft = leader = pgnum = prose = 0
    for t in texts:
        ts = t.strip()
        has_leader = bool(_TOC_LEADER_RE.search(ts))
        if len(ts) <= _TOC_SHORT_MAXLEN or _TOC_NUM_RE.fullmatch(ts) or has_leader:
            short += 1
        ncjk = len(_TOC_CJK_RE.findall(ts))
        if ncjk >= _TOC_LONG_MINLEN:
            longn += 1
        if any(w in ts for w in _TOC_CRAFT_WORDS):
            craft += 1
        if has_leader:
            leader += 1
        if _TOC_PAGEEND_RE.search(ts) or _TOC_CJKPAGEEND_RE.search(ts):
            pgnum += 1
        if _TOC_SENT_RE.search(ts) and ncjk >= _TOC_PROSE_MINLEN:
            prose += 1
    return {"n": n, "short": short / n, "long": longn / n, "craft": craft / n,
            "tocmark": min(1.0, (leader + pgnum) / n), "prose": prose / n}


def _is_anti_toc(ev):
    """反目录证据否决：命中任一「内容表/术语表」特征即判定该页不是目录页。

    (a) 度量/工艺词行占比 ≥25%；
    (b) 或（无足量页码/点线证据 且 ≥20 字长行占比 ≥15%）；
    (c) 或（无足量页码/点线证据 且 叙述性长句行占比 ≥15%）。
    注：(b)(c) 仅在 tocmark<30%（缺目录正面证据）时生效，避免误伤
        「条目 + 点线 + 中文页码」这类古籍真目录形态。
    """
    if ev["craft"] >= _TOC_CRAFT_RATIO:
        return True
    if ev["tocmark"] < _TOC_MARK_RATIO and (
            ev["long"] >= _TOC_LONG_RATIO or ev["prose"] >= _TOC_PROSE_RATIO):
        return True
    return False


def detect_toc_pages(blocks):
    """目录页特征：短行/纯数字为主，或者一页里塞了很多「第N章」标题。

    短行率达标只是候选，还要过 _is_anti_toc 反目录证据否决：含度量/工艺词、
    或（无页码/点线证据且）长行/叙述句偏多的页面（如营造法式「制度」正文表）
    一律不判目录页，避免整页误删正文。
    """
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
        ev = _page_toc_evidence(texts)
        # 短行率达标 → 目录候选；再施反目录证据否决，挡下制度表/术语表这类正文页
        if ev["short"] >= _TOC_SHORT_RATIO and not _is_anti_toc(ev):
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
