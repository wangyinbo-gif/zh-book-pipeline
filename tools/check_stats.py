# -*- coding: utf-8 -*-
"""P2-03 一致性闸门：重算统计并与 README 标记块（STATS-DATA）比对。

用法：
    python tools/check_stats.py                 # README = <root>/README.md（auto）
    python tools/check_stats.py --readme <路径> # 比对任意 README
    python tools/check_stats.py --root <路径>   # 指定工作根（默认 BOOKROOT/tools 上一级）

退出码：
    0  全部一致
    1  不一致（缺块 / 字段缺失 / 数值不同 / rule_version 漂移）——CI 应红
只读：不改任何文件。与 project_stats.py 同目录、同口径（import 复用，避免两套定义）。
"""
import argparse
import io
import json
import os
import sys

# 复用 project_stats 的口径实现（唯一事实源），不重复定义
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_stats as ps  # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# 比对这些字段（STATS-DATA 里的机器可校验字段）
CHECK_FIELDS = ("books", "pages", "characters", "chapters", "figures",
                "paired_figures", "questions", "question_sets",
                "gate_pass", "gate_total", "rule_version")


def extract_block(text):
    """从 README 文本取 STATS-DATA 块，返回 dict；找不到返回 None。"""
    if ps.BEGIN not in text or ps.END not in text:
        return None, "no-anchors"
    i = text.index(ps.BEGIN)
    j = text.index(ps.END) + len(ps.END)
    seg = text[i:j]
    m = ps.DATA_RE.search(seg)
    if not m:
        return None, "no-data-line"
    try:
        return json.loads(m.group(1)), "ok"
    except Exception as e:
        return None, "bad-json:%s" % e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=None)
    ap.add_argument("--readme", default=None,
                    help="README 路径；auto/不给 = <root>/README.md")
    args = ap.parse_args()

    root = ps.root_from_args(args.root)
    rpath = args.readme or os.path.join(root, "README.md")
    if args.readme == "auto":
        rpath = os.path.join(root, "README.md")

    if not os.path.exists(rpath):
        print("FAIL: README 不存在: %s" % rpath)
        return 1
    with io.open(rpath, encoding="utf-8") as f:
        text = f.read()
    inblock, reason = extract_block(text)
    if inblock is None:
        print("FAIL: %s 标记块问题: %s" % (rpath, reason))
        return 1

    stats = ps.compute(root)
    expected = ps.data_dict(stats)

    diffs = []
    for k in CHECK_FIELDS:
        got = inblock.get(k, "<missing>")
        want = expected.get(k)
        if got != want:
            diffs.append((k, want, got))

    if diffs:
        print("FAIL: README 统计块与现算不一致（%s）" % rpath)
        for k, want, got in diffs:
            print("  - %s: README=%s 现算=%s" % (k, got, want))
        print("\n修复：python tools/project_stats.py --readme \"%s\"" % rpath)
        return 1

    print("OK: README 统计块与现算一致（%s）" % rpath)
    print("  books=%s pages=%s characters=%s chapters=%s figures=%s paired=%s "
          "questions=%s(%s套) gate=%s/%s rule=%s"
          % (expected["books"], expected["pages"], expected["characters"],
             expected["chapters"], expected["figures"], expected["paired_figures"],
             expected["questions"], expected["question_sets"],
             expected["gate_pass"], expected["gate_total"], expected["rule_version"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
