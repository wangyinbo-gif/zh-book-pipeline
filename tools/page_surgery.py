# -*- coding: utf-8 -*-
r"""书页手术工具：把「图注/图内标注/脚注/表格数字与正文串行粘连」的页面按 OCR 缓存重组。

2026-11-06 审查《中国建筑史》时固化（当时的手工流程）。

用法：
    $env:PYTHONIOENCODING='utf-8'
    .venv\Scripts\python.exe tools\page_surgery.py fixfile.json

fixfile 结构：
    {
      "book_dir": "{BOOKS}/某书",   # 必填，书目录绝对路径
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
import argparse
from copy import deepcopy
import json
from pathlib import Path
from tempfile import NamedTemporaryFile
import hashlib
import os
import re
import shutil
import sys
import time
from collections import Counter

import repair_preflight
from pipeline import count_chars
from realign_chapters import check_bounds, realign_book

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
    parser = argparse.ArgumentParser(description="逐页书稿修复")
    parser.add_argument("plan")
    parser.add_argument("--describe", action="store_true", help="输出供审核的完整变更回执，不写书稿")
    parser.add_argument("--approval", help="主库写盘的逐批批准回执")
    args = parser.parse_args()
    try:
        return run_plan(args)
    except (OSError, ValueError, KeyError, TypeError, AssertionError) as e:
        print("STOP:", e, file=sys.stderr)
        return 1


def run_plan(args):
    plan_bytes = Path(args.plan).read_bytes()
    fix = json.loads(plan_bytes.decode("utf-8-sig"))
    d = fix.get("book_dir")
    if not d:
        raise ValueError("计划缺少书目录")
    fp = os.path.join(d, "book.json")
    raw = Path(fp).read_bytes()
    before = json.loads(raw.decode("utf-8-sig"))
    actual = repair_preflight.digest(raw)
    expected = fix.get("expected_book_sha256")
    if expected and actual != expected:
        raise ValueError("书稿已变化，计划失效")
    book = deepcopy(before)
    retained = amend(book, fix)
    applying = bool(fix.get("apply")) and not args.describe
    guarded = applying and repair_preflight.is_live(d)
    if args.describe or guarded:
        if guarded and not expected:
            raise ValueError("主库计划必须填写改前书稿 SHA-256")
        if realign_book(book):
            raise ValueError("章锚点出现回退，需核实后重新提交计划")
        check_bounds(book)
        receipt = repair_preflight.describe(d, raw, before, book, plan_bytes, retained)
        if args.describe:
            print(json.dumps(receipt, ensure_ascii=False, indent=2))
        if guarded:
            repair_preflight.require_approval(args.approval, receipt)
    if not applying:
        print("（预演，未写盘）")
        return 0
    return write_book(d, fp, book, actual)


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
    stats = book.setdefault("stats", {})
    stats["chars"] = count_chars(book)
    stats["paragraphs"] = sum(1 for b in blocks if b["type"] == "paragraph")
    figs = [b for b in blocks if b["type"] == "figure"]
    stats["figures"] = len(figs)
    stats["paired"] = sum(1 for b in figs if (b.get("caption") or "").strip())
    retained = {id(block): idx for idx, block in enumerate(blocks)}
    return {idx: retained[id(block)] for idx, block in enumerate(original) if id(block) in retained}


def write_book(d, fp, book, expected):
    raw = Path(fp).read_bytes()
    if expected and hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("写入前发现书稿已变化；请重新核对并重新生成计划")
    base = os.path.join(d, "book.json.bak_%s_手术批" % time.strftime("%Y%m%d_%H%M%S"))
    bak = base
    suffix = 1
    while os.path.exists(bak):
        bak = "%s_%d" % (base, suffix)
        suffix += 1
    # Exclusive creation prevents another process from silently replacing a backup.
    with open(bak, "xb") as target:
        target.write(raw)
    shutil.copystat(fp, bak)
    with NamedTemporaryFile(mode="wb", dir=d, prefix=".surgery-", delete=False) as target:
        tmp = Path(target.name)
        try:
            target.write(repair_preflight.encoded_book(book, raw))
            target.flush()
            os.fsync(target.fileno())
            target.close()
            if Path(fp).read_bytes() != raw:
                raise ValueError("写回之前书稿发生变化")
            os.replace(tmp, fp)
        finally:
            tmp.unlink(missing_ok=True)
    print("已写回；备份", os.path.basename(bak),
          "| blocks", len(book["blocks"]), "chars", book["stats"]["chars"],
          "图", book["stats"]["figures"], "配注", book["stats"]["paired"])
    print("下一步：python tools\\realign_chapters.py \"%s\" && python tools\\pipeline.py recheck \"%s\""
          % (d, os.path.basename(d)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
