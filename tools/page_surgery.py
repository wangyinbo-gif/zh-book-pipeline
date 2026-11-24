# -*- coding: utf-8 -*-
r"""书页手术工具：把「图注/图内标注/脚注/表格数字与正文串行粘连」的页面按 OCR 缓存重组。

2026-11-06 审查《中国建筑史》时固化（当时的手工流程）。

用法：
    $env:PYTHONIOENCODING='utf-8'
    .venv\Scripts\python.exe tools\page_surgery.py fixfile.json

fixfile 结构：
    {
      "book_dir": "<BOOKROOT>/books/<book-id>",   # 必填，书目录绝对路径
      "apply": true,                          # false/缺省 = 只试算不写盘
      "regions": [
        {"page": 136,
         "start_prefix": "先农坛是皇帝祭神农",  # 起始块文本前缀，须全书唯一
         "last_prefix":  "下层象四时",          # 可选：结束块；缺省=该页最后一块
         "blocks": [                            # 替换后的新块序列（按阅读顺序）
            {"type": "paragraph", "y": 140, "col": 0, "text": "…"},
            {"$keep_figure": "fig-157", "page": 136, "y": 646, "caption": "图4一16…"}
         ]}
      ]
    }

要点（血泪）：
  - 所有锚点在任一替换之前解析；区间不得重叠；从后往前替换，下标才不会被自己改动
  - $keep_figure 复用原 figure 块（保留 src/w/h/id），只覆盖 caption/y/page
  - 写盘前打印「净删除/净新增汉字」，必须人工确认删除项只是去重或错字，否则就是丢正文
  - 自动备份 book.json.bak_<时间>_手术批
  - 改完必须跑：realign_chapters.py（章边界）→ pipeline.py recheck → proofread.py
  - 若该书有题库：块数一变就要 qbank.py reanchor，别裸改
"""
import json
import hashlib
import os
import re
import shutil
import sys
import time
from collections import Counter

HAN = re.compile(r"[\u4e00-\u9fff]")


def block_text(b):
    return b.get("text") or b.get("caption") or ""


def bag(blocks):
    c = Counter()
    for b in blocks:
        c.update(HAN.findall(block_text(b)))
        if b.get("type") == "table":
            for row in b.get("rows", []):
                for cell in row:
                    c.update(HAN.findall(str(cell or "")))
    return c


def main():
    if len(sys.argv) < 2:
        print("用法: python tools/page_surgery.py fixfile.json")
        return 1
    with open(sys.argv[1], encoding="utf-8-sig") as source:
        fix = json.load(source)
    d = fix.get("book_dir")
    assert d and os.path.exists(os.path.join(d, "book.json")), "fixfile 的 book_dir 无效"
    fp = os.path.join(d, "book.json")
    expected = fix.get("expected_book_sha256")
    if expected:
        with open(fp, "rb") as source:
            actual = hashlib.sha256(source.read()).hexdigest()
        if actual != expected:
            raise SystemExit("书稿已变化，计划失效；请重新核对并重新生成计划")
    with open(fp, encoding="utf-8-sig") as source:
        book = json.load(source)
    amend(book, fix)
    if not fix.get("apply"):
        print("（未写盘：fixfile 无 apply:true）")
        return 0
    return write_book(d, fp, book, expected)


def amend(book, fix):
    """Apply the plan in memory, returning the new indices of retained blocks."""
    blocks = book["blocks"]
    original = list(blocks)
    before = bag(blocks)

    # 1) 先解析全部锚点（任何替换之前）
    plans = []
    for reg in fix["regions"]:
        pref, page = reg["start_prefix"], reg["page"]
        # 区域已经明确指定原PDF页；同名标题在其他页不构成本页歧义。
        hits = [i for i, b in enumerate(blocks)
                if b.get("page") == page and block_text(b).startswith(pref)]
        assert len(hits) == 1, ("前缀命中不唯一/为零:", pref, hits)
        i0 = hits[0]
        assert blocks[i0].get("page") == page, (i0, blocks[i0].get("page"), page)
        if reg.get("last_prefix"):
            hits2 = [i for i, b in enumerate(blocks) if block_text(b).startswith(reg["last_prefix"])]
            assert len(hits2) == 1, ("尾锚不唯一:", reg["last_prefix"], hits2)
            i1 = hits2[0]
            assert i1 >= i0, (i0, i1)
        else:
            i1 = i0
            while i1 + 1 < len(blocks) and blocks[i1 + 1].get("page") == page:
                i1 += 1
        plans.append((i0, i1, reg))
    plans.sort()
    for a, b in zip(plans, plans[1:]):
        assert a[1] < b[0], ("区域重叠", a[:2], b[:2])

    # 2) 从后往前替换
    for i0, i1, reg in reversed(plans):
        page = reg["page"]
        nb = []
        for spec in reg["blocks"]:
            if spec.get("$keep_figure"):
                fid = spec["$keep_figure"]
                orig = next(b for b in blocks if b.get("type") == "figure" and b.get("id") == fid)
                b = dict(orig)
                for k, v in spec.items():
                    if k != "$keep_figure":
                        b[k] = v
                b["type"] = "figure"
            else:
                b = dict(spec)
                b.setdefault("page", page)
                b.setdefault("col", 0)
            nb.append(b)
        print("区域 p%03d: 块 %d..%d (%d块) -> %d块" % (page, i0, i1, i1 - i0 + 1, len(nb)))
        blocks[i0:i1 + 1] = nb

    after = bag(blocks)
    removed = {k: before[k] - after.get(k, 0) for k in before if before[k] > after.get(k, 0)}
    added = {k: after[k] - before.get(k, 0) for k in after if after[k] > before.get(k, 0)}
    print("净删除汉字:", sum(removed.values()),
          "".join(k * v for k, v in sorted(removed.items()))[:200])
    print("净新增汉字:", sum(added.values()),
          "".join(k * v for k, v in sorted(added.items()))[:120])
    if removed:
        print("!! 有删除：请逐字确认只是去重/错字替换，不是丢正文")
    def count_chars(bks):
        return sum(len(block_text(b)) for b in bks
                   if b.get("type") in ("paragraph", "heading", "table"))

    stats = book.setdefault("stats", {})
    stats["chars"] = count_chars(blocks)
    stats["paragraphs"] = sum(1 for b in blocks if b["type"] == "paragraph")
    figs = [b for b in blocks if b["type"] == "figure"]
    stats["figures"] = len(figs)
    stats["paired"] = sum(1 for b in figs if (b.get("caption") or "").strip())
    retained = {id(block): idx for idx, block in enumerate(blocks)}
    return {idx: retained[id(block)] for idx, block in enumerate(original) if id(block) in retained}


def write_book(d, fp, book, expected):
    if expected:
        with open(fp, "rb") as source:
            if hashlib.sha256(source.read()).hexdigest() != expected:
                raise SystemExit("写入前发现书稿已变化；请重新核对并重新生成计划")
    base = os.path.join(d, "book.json.bak_%s_手术批" % time.strftime("%Y%m%d_%H%M%S"))
    bak = base
    suffix = 1
    while os.path.exists(bak):
        bak = "%s_%d" % (base, suffix)
        suffix += 1
    # Exclusive creation prevents another process from silently replacing a backup.
    with open(fp, "rb") as source, open(bak, "xb") as target:
        shutil.copyfileobj(source, target)
    shutil.copystat(fp, bak)
    tmp = fp + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(book, f, ensure_ascii=False)
    os.replace(tmp, fp)
    print("已写回；备份", os.path.basename(bak),
          "| blocks", len(book["blocks"]), "chars", book["stats"]["chars"],
          "图", book["stats"]["figures"], "配注", book["stats"]["paired"])
    print("下一步：python tools\\realign_chapters.py \"%s\" && python tools\\pipeline.py recheck \"%s\""
          % (d, os.path.basename(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
