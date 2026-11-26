# -*- coding: utf-8 -*-
"""project_stats.py / check_stats.py 统计口径回归测试。

用临时目录造假库，不依赖真实 books/，可反复重跑（无副作用、无第三方依赖）。
运行：python -m unittest tools.test_project_stats   或   python tools/test_project_stats.py
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import project_stats as ps  # noqa: E402
import check_stats as cs    # noqa: E402


def wj(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False))


def wf(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def rf(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def make_lib(root):
    """A 书：stats.chapters=2 但 chapters 数组=3（模拟《城市意象》分叉）；B 书一致。
    题库：A 正式件 5 题 + 备份件 99 题（旧口径会误吞备份）；B 只有备份件。"""
    wj(os.path.join(root, "books", "A", "book.json"), {
        "bookTitle": "A", "shelfCategory": "教材",
        "stats": {"pages": 100, "chars": 5000, "chapters": 2, "figures": 10, "paired": 10},
        "chapters": [{"title": "第1章"}, {"title": "第2章"}, {"title": "附录"}],
    })
    wj(os.path.join(root, "books", "A", "质检.json"), {"checkedAt": "2026-01-01 10:00", "gate": {"ok": True}})
    wj(os.path.join(root, "books", "B", "book.json"), {
        "bookTitle": "B", "shelfCategory": "扩展",
        "stats": {"pages": 50, "chars": 2000, "chapters": 3, "figures": 4, "paired": 2},
        "chapters": [{"title": "x"}, {"title": "y"}, {"title": "z"}],
    })
    wj(os.path.join(root, "books", "B", "质检.json"), {"checkedAt": "2026-01-02 09:00", "gate": {"ok": False}})
    wj(os.path.join(root, "题库", "A.json"), {"bookId": "A", "questions": [{"q": i} for i in range(5)]})
    wj(os.path.join(root, "题库", "_backups", "A.json"), {"bookId": "A", "questions": [{"q": i} for i in range(99)]})
    wj(os.path.join(root, "题库", "_backups", "B.json"), {"bookId": "B", "questions": [{"q": i} for i in range(7)]})


def check(rpath, root):
    """复用 check_stats 的比较逻辑（绕开 argv）。返回 0=一致 / 1=不一致。"""
    block, _reason = cs.extract_block(rf(rpath))
    if block is None:
        return 1
    exp = ps.data_dict(ps.compute(root))
    for k in cs.CHECK_FIELDS:
        if block.get(k, "<missing>") != exp.get(k):
            return 1
    return 0


class ProjectStatsTest(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        self.root = d.name
        make_lib(self.root)
        self.stats = ps.compute(self.root)

    def test_chapters_authoritative_is_stats_field(self):
        # 权威 = Σ stats.chapters = 2+3 = 5，不是数组和 3+3=6
        self.assertEqual(self.stats["chapters"], 5)
        self.assertEqual(self.stats["diagnostics"]["chapters_array_sum"], 6)
        self.assertEqual(self.stats["diagnostics"]["chapters_stats_vs_array_delta"], 1)
        self.assertEqual(self.stats["diagnostics"]["chapters_field_mismatch_books"], ["A"])

    def test_bank_excludes_backups(self):
        # A 只认正式件 5 题（不被备份 99 污染）；B 只有备份件 → 不计入
        self.assertEqual(self.stats["questions"], 5)
        self.assertEqual(self.stats["question_sets"], 1)

    def test_books_pages_chars_figures(self):
        self.assertEqual(self.stats["books"], 2)
        self.assertEqual(self.stats["pages"], 150)
        self.assertEqual(self.stats["characters"], 7000)
        self.assertEqual(self.stats["figures"], 14)
        self.assertEqual(self.stats["paired_figures"], 12)

    def test_gate_summary(self):
        gs = self.stats["gate_status_summary"]
        self.assertEqual(gs["pass"], 1)          # 只有 A 绿
        self.assertEqual(gs["total_with_qc"], 2)
        self.assertFalse(gs["all_pass"])
        self.assertEqual(gs["latest_checked"], "2026-01-02 09:00")

    def test_schema_fields_present(self):
        for k in ("generated_at", "stats_schema_version", "rule_version", "rule",
                  "books", "pages", "characters", "chapters", "figures",
                  "paired_figures", "questions", "gate_status_summary"):
            self.assertIn(k, self.stats)
        self.assertEqual(self.stats["stats_schema_version"], "1.0")

    def test_idempotent(self):
        a = dict(self.stats)
        b = dict(ps.compute(self.root))
        a.pop("generated_at")
        b.pop("generated_at")
        self.assertEqual(a, b)

    def test_readme_bootstrap_roundtrip(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "# 标题\n实盘规模（旧）：**0 本**。\n结尾\n")
        op, changed = ps.apply_readme(rp, self.stats, bootstrap=True)
        self.assertEqual(op, "bootstrap")
        self.assertTrue(changed)
        text = rf(rp)
        self.assertIn(ps.BEGIN, text)
        self.assertIn(ps.END, text)
        self.assertNotIn("实盘规模（旧）", text)   # 旧自由文本行被整块替换
        self.assertEqual(check(rp, self.root), 0)  # check_stats 判一致
        # 二次跑（已带锚点）= replace 且无变化（幂等）
        op2, changed2 = ps.apply_readme(rp, self.stats)
        self.assertEqual(op2, "replace")
        self.assertFalse(changed2)

    def test_apply_without_anchor_or_legacy_raises(self):
        rp = os.path.join(self.root, "R2.md")
        wf(rp, "这是一个完全没有锚点、也没有旧统计行的文件\n")
        with self.assertRaises(SystemExit):        # 无锚点、无 bootstrap
            ps.apply_readme(rp, self.stats)
        with self.assertRaises(SystemExit):        # 有 bootstrap 但找不到「实盘规模」旧行
            ps.apply_readme(rp, self.stats, bootstrap=True)

    def test_check_detects_mismatch(self):
        rp = os.path.join(self.root, "README.md")
        wf(rp, "实盘规模（旧）：x\n")
        ps.apply_readme(rp, self.stats, bootstrap=True)
        bad = rf(rp).replace('"chapters": 5', '"chapters": 9')
        wf(rp, bad)
        self.assertEqual(check(rp, self.root), 1)

    def test_snapshot_refuses_overwrite(self):
        fp = ps.write_snapshot(self.root, "vT", self.stats)
        self.assertTrue(os.path.exists(fp))
        with self.assertRaises(SystemExit):
            ps.write_snapshot(self.root, "vT", self.stats)   # 已存在 → 拒绝覆盖


if __name__ == "__main__":
    unittest.main(verbosity=2)
