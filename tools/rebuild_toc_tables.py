# -*- coding: utf-8 -*-
"""按各书真实目录重建章节（页码来自书前目录页 + 正文锚点/偏移）。"""
from paths import BOOKS
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
BOOKS = BOOKS

# (标题, PDF 起始页)
TABLES = {
    "外部空间设计": [
        ("第一章 外部空间的基本概念", 19),
        ("第二章 外部空间的要素", 57),
        ("第三章 外部空间的设计手法", 85),
        ("第四章 空间秩序的建立", 147),
    ],
    "建筑语汇": [
        ("第一章 序言", 13),
        ("第二章 理论", 16),
        ("第三章 功能分区", 43),
        ("第四章 建筑空间", 73),
        ("第五章 交通流线与建筑造型", 93),
        ("第六章 有关基地的考虑因素", 123),
        ("第七章 建筑物结构体", 167),
    ],
    "建筑构成学": [
        ("第一章 原论", 19),
        ("第二章 由室与架构形成的住宅构成", 33),
        ("第三章 由室群与体量形成的建筑构成", 68),
        ("第四章 由建筑形成的外部构成", 133),
        ("第五章 由建筑配列形成的体量的集合构成", 160),
        ("第六章 由建筑集合形成的城市空间构成", 184),
    ],
    "美国大城市的死与生": [
        ("导言", 6),
        ("第一章 人行道的用途：安全", 34),
        ("第二章 人行道的用途：交往", 63),
        ("第三章 人行道的用途：孩子的同化", 85),
        ("第四章 街区公园的用途", 101),
        ("第五章 城市街区的用途", 127),
        ("第六章 产生多样性的因素", 162),
        ("第七章 主要用途混合之必要性", 172),
        ("第八章 小街段之必要", 202),
        ("第九章 老建筑之必要", 212),
        ("第十章 密度之需要", 226),
        ("第十一章 有关多样性的一些神话", 249),
        ("第十二章 多样性的自我毁灭", 270),
        ("第十三章 交界真空带的危害", 288),
        ("第十四章 非贫民区化和贫民区化", 303),
        ("第十五章 渐次性资金和急剧性资金", 329),
        ("第十六章 对住宅的资助", 362),
        ("第十七章 被蚕食的城市与对汽车的限制", 381),
        ("第十八章 视觉秩序：局限性和可能性", 421),
        ("第十九章 拯救和利用廉租住宅区", 445),
        ("第二十章 地区管理和规划", 459),
        ("第二十一章 城市的问题所在", 485),
        ("致谢", 509),
        ("索引", 511),
    ],
    "拼贴城市": [
        ("译者前言", 8),
        ("引言", 17),
        ("乌托邦：衰落并消亡？", 22),
        ("太平盛世之后", 44),
        ("实体的危机：肌理的困境", 62),
        ("冲突城市以及“拼贴匠”的政治", 95),
        ("拼贴城市以及时间的再征服", 129),
        ("附录", 160),
        ("注释", 190),
        ("名词索引", 194),
        ("图片索引", 195),
    ],
}


def rebuild(key, table, dry=False):
    import glob
    d = [x for x in glob.glob(os.path.join(BOOKS, "*")) if os.path.isdir(x) and key in x][0]
    book = json.load(open(os.path.join(d, "book.json"), encoding="utf-8"))
    blocks = book["blocks"]
    pages = [b.get("page") or 0 for b in blocks]
    n = len(blocks)

    def first_idx(page):
        for i in range(n):
            if pages[i] >= page:
                return i
        return n

    # 在目标页 ±8 内找章节标题首次出现，吸附到真实章首页
    def norm(x):
        import re
        return re.sub(r"[^\u4e00-\u9fff]", "", x or "")

    def snap(title, page):
        import re as _re
        key = norm(_re.sub(r"^第[0-9一二三四五六七八九十]+章", "", title))
        if len(key) < 3:
            return page
        best = None
        for i in range(n):
            pg = pages[i]
            if pg < page - 8 or pg > page + 8:
                continue
            if key[:5] in norm(blocks[i].get("text")):
                best = pg
                break
        return best if best is not None else page

    toc_last = 0
    for i in range(n):
        if "目录" in (blocks[i].get("text") or "") or "目次" in (blocks[i].get("text") or ""):
            toc_last = max(toc_last, pages[i])

    def snap2(title, page):
        got = snap(title, page)
        if toc_last and got <= toc_last:
            return page
        return got

    pages_t = list(table)   # 目录页页码已按书前目录 + 正文锚点校过，不再吸附
    idxs = [first_idx(pg) for _t, pg in pages_t]
    # 保证严格递增
    for i in range(1, len(idxs)):
        if idxs[i] <= idxs[i - 1]:
            idxs[i] = min(n, idxs[i - 1] + 1)
    out = []
    pre = idxs[0]
    if pre > 0:
        chunk = blocks[:pre]
        out.append({"id": "ch-pre", "title": "前言与序", "startBlock": 0,
                    "chars": sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading")),
                    "figures": sum(1 for b in chunk if b.get("type") == "figure"), "blocks": chunk})
    for i, (title, _p) in enumerate(pages_t):
        s = idxs[i]
        e = idxs[i + 1] if i + 1 < len(idxs) else n
        chunk = blocks[s:e]
        out.append({"id": "ch-%d" % len(out), "title": title, "startBlock": s,
                    "chars": sum(len(b.get("text") or "") for b in chunk if b.get("type") in ("paragraph", "heading")),
                    "figures": sum(1 for b in chunk if b.get("type") == "figure"), "blocks": chunk})
    # 末尾空章并入上一章
    while len(out) > 1 and out[-1]["chars"] < 20:
        last = out.pop()
        out[-1]["blocks"] = out[-1].get("blocks", []) + last.get("blocks", [])
        out[-1]["chars"] += last.get("chars", 0)
        out[-1]["figures"] += last.get("figures", 0)
    empty = [c["title"] for c in out if c["chars"] < 20]
    print("== %-14s 章 %d | 空章 %d %s" % (key, len(out), len(empty), empty[:3]))
    for c in out[:6]:
        print("     p%-5s %-34s %d 字" % (pages[c["startBlock"]], c["title"], c["chars"]))
    if dry:
        return
    book["chapters"] = out
    book["stats"]["chapters"] = len(out)
    json.dump(book, open(os.path.join(d, "book.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("     已写入")


def main():
    dry = "--dry" in sys.argv
    keys = [a for a in sys.argv[1:] if not a.startswith("--")] or list(TABLES)
    for k in keys:
        rebuild(k, TABLES[k], dry)


if __name__ == "__main__":
    main()
