# -*- coding: utf-8 -*-
"""重建这几本书的目录（章首对齐到真正的标题块，标题清洗掉页码/索引噪声）。"""
from paths import BOOKS
import glob
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

B = BOOKS
CH = re.compile(r"^第\s*([0-9一二三四五六七八九十百零〇]{1,3})\s*章")
DIG = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
norm = lambda t: re.sub(r"\s+", "", t or "")


def cn2int(s):
    if s.isdigit():
        return int(s)
    if s == "十":
        return 10
    if "十" in s:
        a, b = s.split("十")
        return (DIG.get(a, 1) if a else 1) * 10 + (DIG.get(b, 0) if b else 0)
    return DIG.get(s, -1)


def toc_pages(blocks, limit=40):
    per = {}
    for b in blocks:
        if b.get("type") != "heading":
            continue
        m = CH.match(norm(b.get("text")))
        if m and (b.get("page") or 0) <= limit:
            per.setdefault(b.get("page"), set()).add(cn2int(m.group(1)))
    return {p for p, s in per.items() if len(s) >= 4}


def clean_title(t, n):
    t = norm(t)
    m = CH.match(t)
    if m:
        t = t[m.end():]
    t = re.sub(r"\\?\d{2,}.*$", "", t)
    t = re.sub(r"[A-Za-z]{3,}.*$", "", t)
    t = t.strip(" ·—-:：、,，")
    return "第%d章%s" % (n, (" " + t) if t else "")


def snap(blocks, idx, title, n):
    """把起点吸附到附近的同号标题块。"""
    lo, hi = max(0, idx - 20), min(len(blocks) - 1, idx + 40)
    best = None
    for i in range(lo, hi + 1):
        b = blocks[i]
        if b.get("type") != "heading":
            continue
        t = norm(b.get("text"))
        m = CH.match(t)
        if not m or cn2int(m.group(1)) != n:
            continue
        if best is None or abs(i - idx) < abs(best - idx):
            best = i
    return best if best is not None else idx


def build(book, mode, manual=None):
    blocks = book["blocks"]
    if mode == "manual":
        starts = []
        for title, page in manual:
            idx = next((i for i, b in enumerate(blocks) if (b.get("page") or 0) >= page), len(blocks) - 1)
            m = re.search(r"第(\d+)章", title)
            if m:
                idx = snap(blocks, idx, title, int(m.group(1)))
            starts.append((idx, title))
    else:
        skip = toc_pages(blocks)
        starts = []
        last_n, last_page = 0, -1
        for i, b in enumerate(blocks):
            if b.get("type") != "heading" or b.get("page") in skip:
                continue
            m = CH.match(norm(b.get("text")))
            if not m:
                continue
            n = cn2int(m.group(1))
            if n != last_n + 1 or (b.get("page") or 0) <= last_page:
                continue
            starts.append((i, clean_title(b.get("text"), n)))
            last_n, last_page = n, b.get("page")
        if starts and starts[0][0] > 0:
            starts.insert(0, (0, "前言与序"))
    chapters = []
    for k, (sb, title) in enumerate(starts):
        end = starts[k + 1][0] if k + 1 < len(starts) else len(blocks)
        chunk = blocks[sb:end]
        chapters.append({"id": "ch-%d" % k, "title": title, "startBlock": sb,
                         "chars": sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading")),
                         "figures": sum(1 for b in chunk if b.get("type") == "figure"),
                         "blocks": chunk})
    book["chapters"] = chapters
    book["stats"]["chapters"] = len(chapters)


PLAN = [
    ("中国建筑史 ", "auto", None),
    ("外国近现代建筑史(第三版)(上)", "manual", [
        ("前言与序", 1),
        ("第1章 18世纪下半叶至19世纪下半叶欧洲与美国的建筑", 18),
        ("第2章 19世纪下半叶至20世纪初对新建筑的探求", 42),
        ("第3章 现代建筑走向成熟——两次世界大战之间的探索与发展", 62),
        ("第4章 第二次世界大战后至1970年代的建筑发展", 222)]),
    ("外国近现代建筑史(第三版)(下)", "manual", [
        ("前言与序", 1),
        ("第五章 现代主义之后的建筑思潮与实践", 4)]),
    ("建筑批评学", "auto", None),
    ("负建筑", "manual", [
        ("前言与序", 1),
        ("序（朱锷）", 13),
        ("自序", 18),
        ("Ⅰ-1 从分割到统一", 29),
        ("Ⅰ-2 场和物", 44),
        ("Ⅰ-3 何谓批判性？", 68),
        ("Ⅰ-4 形式与自由", 83),
        ("Ⅱ-1 透明得让人倍感凄凉的风格派", 105),
        ("Ⅱ-2 辛德勒：民主主义的理想", 117),
        ("Ⅱ-3 内田祥哉：二战后的民主主义", 129),
        ("Ⅱ-4 村野藤吾：制度和唯物论", 136),
        ("Ⅱ-5 三爱梦幻中心：场所、建筑物、表象", 151),
        ("Ⅱ-6 威尼斯双年展1995：给我一个家，让我看电视", 157),
        ("Ⅱ-7 威尼斯双年展2000：少女与行者", 169),
        ("Ⅲ-1 正统、品牌、个性", 173),
        ("Ⅲ-2 风俗住宅", 183),
        ("Ⅲ-3 混凝土时代", 188),
        ("Ⅲ-4 虚拟世界", 191),
        ("Ⅲ-5 “美”的终结", 199),
        ("Ⅲ-6 圈地运动", 204),
        ("结束语", 213),
        ("出处一览", 217)]),
    ("公共建筑设计原理 (刘云月", "manual", [
        ("前言与序", 1),
        ("第1章 空间与形式直观", 16),
        ("第2章 形式与空间构成", 40),
        ("第4章 高层建筑方案设计要略", 178),
        ("第5章 思维与工具：20世纪设计中三大整体性理论", 230),
        ("第6章 决策与评价：方案设计阶段三大经济学视野", 258),
        ("第3章 建筑方案设计原理与方法", 297)]),
]


def main():
    for key, mode, manual in PLAN:
        ds = [x for x in glob.glob(os.path.join(B, "*")) if os.path.isdir(x) and key in x]
        if not ds:
            print("!!", key)
            continue
        d = ds[0]
        bj = os.path.join(d, "book.json")
        book = json.load(open(bj, encoding="utf-8"))
        build(book, mode, manual)
        json.dump(book, open(bj, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("== %-22s 章 %d" % ((book.get("title") or "")[:20], len(book["chapters"])))
        for c in book["chapters"]:
            first = c["blocks"][0] if c["blocks"] else {}
            print("   p%-4s %-40s | 首块 %r" % (first.get("page"), c["title"][:38], (first.get("text") or "")[:22]))


if __name__ == "__main__":
    main()
