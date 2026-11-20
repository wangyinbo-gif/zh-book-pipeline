# -*- coding: utf-8 -*-
"""筑学 · 阅读体验修补（第二轮）

1. 去掉最后一页（出版社广告/分类表）；
2. 去掉误检成插图的「篇扉页扫描图」（无图注、宽度接近整页）；
3. 给个别真插图补图注；
4. 清理 OCR 重复标点（。，。. 之类）；
5. 把「逗号后断段」「续段」并回上一段（只做确定性高的规则）；
6. 重建章节/统计/预览。
"""
import json
import os
import re
import sys
import glob

TERM = "。！？…：；”』」)"
CN = re.compile(r"[\u4e00-\u9fa5]")
CONT = re.compile(r"^(的|地|得|了|着|过|们|[（(]|[，、）)]|[a-z0-9])")


def width_of(book_dir, src):
    try:
        from PIL import Image
        with Image.open(os.path.join(book_dir, src.replace("/", os.sep))) as im:
            return im.size[0]
    except Exception:
        return 0


def drop_ad_page(blocks):
    return [b for b in blocks if b.get("page") != 414]


def drop_bad_figures(blocks, book_dir):
    """删掉误检的篇扉页扫描图（无图注且宽度≥整页宽度的 90%），并删资源文件。"""
    removed = []
    keep = []
    for b in blocks:
        if b["type"] == "figure" and not b.get("caption"):
            w = width_of(book_dir, b.get("src") or "")
            if w >= 380:
                removed.append(b)
                p = os.path.join(book_dir, (b.get("src") or "").replace("/", os.sep))
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                    except OSError:
                        pass
                continue
        keep.append(b)
    return keep, removed


def fix_captions(blocks):
    fixed = 0
    for b in blocks:
        if b["type"] != "figure":
            continue
        if b.get("id") == "fig-220" and not b.get("caption"):
            b["caption"] = "图9-16 恩瓦立德教堂平面"
            b["displayCaption"] = (b.get("displayCaption") or "图212") + " 恩瓦立德教堂平面"
            fixed += 1
    return fixed


PUNCT_FIX = [
    ("。，", "。"), ("。、", "。"), ("。.", "。"), ("。。", "。"),
    ("。，", "。"), ("，。", "。"), ("．。", "。"),
]


def clean_punct(blocks):
    n = 0
    for b in blocks:
        if b["type"] != "paragraph":
            continue
        t = b["text"]
        for a, c in PUNCT_FIX:
            if a in t:
                t = t.replace(a, c)
        if t != b["text"]:
            b["text"] = t
            n += 1
    return n


def merge_runs(blocks):
    """确定性高的续段合并：上一段以逗号/顿号收尾，或下一段以明显续接词开头。"""
    merged = 0
    changed = True
    while changed:
        changed = False
        out = []
        i = 0
        while i < len(blocks):
            b = blocks[i]
            if b["type"] == "paragraph" and out:
                prev = out[-1]
                if prev["type"] == "paragraph":
                    pt = (prev.get("text") or "").strip()
                    ct = (b.get("text") or "").strip()
                    if pt and ct:
                        last = pt[-1]
                        merge = False
                        if last in "，、":
                            merge = True
                        elif last in "）)" and CONT.match(ct):
                            merge = False
                        elif CN.match(last) and CONT.match(ct):
                            merge = True
                        elif re.match(r"[0-9A-Za-z]", last) and re.match(r"^(的|[（(]|[a-z])", ct):
                            merge = True
                        if merge:
                            prev["text"] = pt + ct
                            pages = set(prev.get("pages") or [prev.get("page")])
                            pages.update(b.get("pages") or [b.get("page")])
                            prev["pages"] = sorted(p for p in pages if p)
                            merged += 1
                            changed = True
                            i += 1
                            continue
            out.append(b)
            i += 1
        blocks = out
    return blocks, merged




CAP_LIKE_TAIL = re.compile(r"(年|平面|剖面|立面|内部|内景|全景|教堂|府邸|塔|广场|图|院|墓|宫)$")


def is_caption_like(t):
    t = (t or "").strip()
    if not t or len(t) > 34:
        return False
    if re.search(r"[。！？]", t):
        return False
    if t.startswith("图"):
        return True
    if ("（" in t or "）" in t) and len(t) <= 30:
        return True
    return bool(CAP_LIKE_TAIL.search(t))


def should_merge(prev, nxt):
    pt = (prev.get("text") or "").strip()
    ct = (nxt.get("text") or "").strip()
    if not pt or not ct:
        return False
    last = pt[-1]
    if last in "，、":
        return True
    if CN.match(last) and CONT.match(ct):
        return True
    if re.match(r"[0-9A-Za-z]", last) and re.match(r"^(的|[（(]|[a-z])", ct):
        return True
    return False


def merge_across_figures(blocks):
    """段落之间隔着插图时：图注样式的短行并进图注，续段并回上一段。"""
    res = []
    moved = merged = 0
    i = 0
    while i < len(blocks):
        b = blocks[i]
        if b["type"] != "paragraph":
            res.append(b)
            i += 1
            continue
        j = i + 1
        figs = []
        while j < len(blocks) and blocks[j]["type"] == "figure":
            figs.append(blocks[j])
            j += 1
        nxt = blocks[j] if j < len(blocks) else None
        if figs and nxt is not None and nxt["type"] == "paragraph":
            if is_caption_like(nxt.get("text")):
                f = figs[-1]
                f["caption"] = ((f.get("caption") or "") + " " + (nxt.get("text") or "").strip()).strip()
                moved += 1
                res.append(b)
                res.extend(figs)
                i = j + 1
                continue
            if should_merge(b, nxt):
                b["text"] = (b.get("text") or "").strip() + (nxt.get("text") or "").strip()
                pages = set(b.get("pages") or [b.get("page")])
                pages.update(nxt.get("pages") or [nxt.get("page")])
                b["pages"] = sorted(p for p in pages if p)
                merged += 1
                res.append(b)
                res.extend(figs)
                i = j + 1
                continue
        res.append(b)
        i += 1
    return res, moved, merged




FOOT = ("①", "②", "③", "④", "⑤", "⑥", "⑦", "⑧", "⑨", "⑩")
REF_RE = re.compile(r"\[[MJC]\]|^[A-Z][A-Za-z&.,;:\-\s’'\"()]{4,}|^[A-Z]{2,}")


def is_reference(t):
    t = (t or "").strip()
    if not t:
        return False
    if REF_RE.search(t):
        return True
    letters = sum(1 for ch in t if ch.isascii() and ch.isalpha())
    return len(t) >= 8 and letters / len(t) > 0.45


def merge_midsentence(blocks):
    """上一段没有句末标点：短行升级成小标题，其余并回上一段（允许中间隔着插图）。"""
    TERM = "。！？…：；”』」）)】.．!?"
    out = []
    merged = headed = 0
    for b in blocks:
        if b["type"] != "paragraph":
            out.append(b)
            continue
        ct = (b.get("text") or "").strip()
        target = None
        for k in range(len(out) - 1, max(-1, len(out) - 4), -1):
            if out[k]["type"] == "paragraph":
                target = k
                break
        if target is not None:
            prev = out[target]
            pt = (prev.get("text") or "").strip()
            if (pt and ct and len(pt) >= 20 and pt[-1] not in TERM
                    and not ct.startswith(FOOT) and not is_reference(ct)):
                if len(ct) < 18 and not any(ch in ct for ch in "。！？"):
                    nb = dict(b)
                    nb["type"] = "heading"
                    out.append(nb)
                    headed += 1
                    continue
                prev["text"] = pt + ct
                pages = set(prev.get("pages") or [prev.get("page")])
                pages.update(b.get("pages") or [b.get("page")])
                prev["pages"] = sorted(p for p in pages if p)
                merged += 1
                continue
        out.append(b)
    return out, merged, headed



def merge_all_midsentence(blocks):
    """按段落顺序，合并中间只隔着插图/跨页、且没有小标题打断的续段。"""
    TERM = "。！？…：；”』」）)】.．!?"
    out = list(blocks)
    merged = 0
    i = 0
    while i < len(out):
        if out[i]["type"] == "paragraph":
            j = i + 1
            blocked = False
            while j < len(out) and out[j]["type"] != "paragraph":
                if out[j]["type"] == "heading":
                    blocked = True
                    break
                j += 1
            if not blocked and j < len(out):
                pt = (out[i].get("text") or "").strip()
                ct = (out[j].get("text") or "").strip()
                if (pt and pt[-1] not in TERM and len(pt) >= 20 and ct
                        and not ct.startswith(FOOT) and not is_reference(ct)
                        and not label_like(ct) and len(ct) >= 2):
                    out[i]["text"] = pt + ct
                    pages = set(out[i].get("pages") or [out[i].get("page")])
                    pages.update(out[j].get("pages") or [out[j].get("page")])
                    out[i]["pages"] = sorted(p for p in pages if p)
                    out.pop(j)
                    merged += 1
                    continue
        i += 1
    return out, merged


def label_like(t):
    t = (t or "").strip()
    if not t or len(t) > 120:
        return False
    if any(ch in t for ch in "。！？"):
        return False
    if len(re.findall(r"[（(][a-zA-Z0-9][）)]", t)) >= 2:
        return True
    if t.count("；") >= 2 and len(t) < 90:
        return True
    if re.fullmatch(r"[A-Za-z0-9\s.,;:，。；：\-—=+*/%°㎡]+", t):
        return True
    if len(t) <= 40 and (sum(1 for ch in t if ch.isdigit()) >= 2 or "图" in t):
        return True
    func = sum(1 for ch in t if ch in "的了是在和与有为也就都而及或等")
    return len(t) <= 30 and func <= 1



def absorb_label_paragraphs(blocks):
    """把紧挨插图的“图注/标注碎行”并入该图图注，避免它们当正文造成断句。"""
    figs = [b for b in blocks if b["type"] == "figure"]
    moved = 0
    out = []
    for b in blocks:
        if b["type"] == "paragraph":
            t = (b.get("text") or "").strip()
            pg = b.get("page")
            cands = [f for f in figs if f.get("page") == pg]
            if cands and len(t) <= 50 and label_like(t):
                y = b.get("y") or 0
                f = min(cands, key=lambda x: abs((x.get("y") or 0) - y))
                f["caption"] = ((f.get("caption") or "") + " " + t).strip()
                moved += 1
                continue
        out.append(b)
    return out, moved


CHAP_RE = re.compile(r"^第\s?([0-9一二三四五六七八九十百零两]+)\s?([篇章])")


def rebuild(book, blocks):
    heads = [i for i, b in enumerate(blocks)
             if b["type"] == "heading" and CHAP_RE.match(b.get("text") or "")]
    chapters = []
    first = heads[0] if heads else len(blocks)
    if blocks[:first]:
        chapters.append({"id": "ch-0", "title": "前言与序", "startBlock": 0, "blocks": blocks[:first]})
    for k, start in enumerate(heads):
        end = heads[k + 1] if k + 1 < len(heads) else len(blocks)
        ch_blocks = blocks[start + 1:end]
        if not ch_blocks:
            continue
        chapters.append({"id": f"ch-{len(chapters)}", "title": blocks[start]["text"].strip(),
                         "startBlock": start, "blocks": ch_blocks})
    for ch in chapters:
        pages = [b.get("page") for b in ch["blocks"] if b.get("page")]
        ch["startPage"] = min(pages) if pages else None
        ch["chars"] = sum(len(b.get("text") or "") for b in ch["blocks"] if b["type"] == "paragraph")
        ch["figures"] = sum(1 for b in ch["blocks"] if b["type"] == "figure")
    book["blocks"] = blocks
    book["chapters"] = chapters
    figs = [b for b in blocks if b["type"] == "figure"]
    paras = [b for b in blocks if b["type"] == "paragraph"]
    book["stats"] = {
        "pages": book.get("stats", {}).get("pages", 0),
        "chars": sum(len(b.get("text") or "") for b in paras),
        "paragraphs": len(paras),
        "figures": len(figs),
        "paired": sum(1 for f in figs if f.get("caption")),
        "chapters": len(chapters),
    }


def main():
    book_dir = sys.argv[1]
    path = os.path.join(book_dir, "book.json")
    with open(path, encoding="utf-8") as f:
        book = json.load(f)
    blocks = book["blocks"]

    blocks = drop_ad_page(blocks)
    blocks, removed = drop_bad_figures(blocks, book_dir)
    capn = fix_captions(blocks)
    pn = clean_punct(blocks)
    blocks, mn = merge_runs(blocks)
    blocks, cap_moved, cross_merged = merge_across_figures(blocks)
    blocks, mid_merged, mid_headed = merge_midsentence(blocks)
    label_moved = 0  # 标注吸收已停用（容易误吞正文）
    blocks, all_merged = merge_all_midsentence(blocks)
    rebuild(book, blocks)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False, indent=2)
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from book_preview import build_preview
        build_preview(book, os.path.join(book_dir, "preview.html"))
    except Exception as e:
        print("预览重建失败:", e)
    print("删除广告页块 + 误检图:", len(removed), "| 补图注:", capn, "| 修标点:", pn, "| 合并续段:", mn)
    print("跨图注行并入图注:", cap_moved, "| 跨图续段合并:", cross_merged,
          "| 句中接段:", mid_merged, "| 升级小标题:", mid_headed,
          "| 标注并入图注:", label_moved, "| 全量接段:", all_merged)
    print("章节:", len(book["chapters"]), "| 段落:", book["stats"]["paragraphs"], "| 图:", book["stats"]["figures"], "| 字数:", book["stats"]["chars"])


if __name__ == "__main__":
    main()
