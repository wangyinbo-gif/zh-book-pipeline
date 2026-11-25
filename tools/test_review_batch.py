"""Safety tests for staged batch preparation and application on disposable books."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import page_surgery
import review_batch


def run_cli(*args):
    result = subprocess.run(
        [sys.executable, *args],
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
    )
    # Decode in the test thread so an unexpected encoding fails the test.
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout=result.stdout.decode("utf-8"),
        stderr=result.stderr.decode("utf-8"),
    )


class ReviewBatchTests(unittest.TestCase):
    def setUp(self):
        test_root = (Path(__file__).resolve().parent.parent / "_工作记录").resolve()
        test_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="review_batch_test_", dir=test_root)
        self.root = Path(self.temp.name).resolve()
        if self.root.parent != test_root:
            raise RuntimeError("测试目录不在指定工作区中")
        self.bookdir = self.root / "book"
        self.bookdir.mkdir()
        self.bookfile = self.bookdir / "book.json"
        self.book = {"stats": {}, "blocks": [
            {"type": "paragraph", "page": 1, "text": "一段正文甲。"},
            {"type": "table", "page": 2, "caption": "表一", "rows": [["原有内容", "保留"]]},
        ]}
        self.write_book()
        self.evidence = self.root / "p1.png"
        self.evidence.write_bytes(b"test evidence")

    def tearDown(self):
        self.temp.cleanup()

    def write_book(self):
        self.bookfile.write_text(json.dumps(self.book, ensure_ascii=False), encoding="utf-8")

    def manifest(self, regions, reviews):
        plan = self.root / "plan.json"
        plan.write_text(json.dumps({"book_dir": str(self.bookdir), "regions": regions}, ensure_ascii=False), encoding="utf-8")
        manifest = self.root / "manifest.json"
        manifest.write_text(json.dumps({"book_dir": str(self.bookdir), "items": [{"plan": "plan.json", "reviews": reviews}]}, ensure_ascii=False), encoding="utf-8")
        return manifest

    def test_table_cells_are_counted_and_unexplained_deletion_blocks_ready(self):
        old = self.book["blocks"][1]
        new = dict(old, rows=[["原有", "保留"]])
        reg = {"page": 2, "start_prefix": "表一", "last_prefix": "表一", "blocks": [new]}
        review = {"evidence": [str(self.evidence)], "verified": True}
        manifest = self.manifest([reg], [review])
        dry, report = review_batch.compile_batch(manifest)
        self.assertFalse(dry["apply"])
        self.assertEqual(report["regions"][0]["removed_hanzi"], {"内": 1, "容": 1})
        with self.assertRaisesRegex(ValueError, "净删汉字"):
            review_batch.compile_batch(manifest, ready=True)
        review["deletion_explanation"] = "已核对，测试删除"
        merged, _ = review_batch.compile_batch(self.manifest([reg], [review]), ready=True)
        self.assertTrue(merged["apply"])

    def test_unreviewed_and_overlapping_regions_are_rejected(self):
        reg = {"page": 1, "start_prefix": "一段正文甲", "last_prefix": "一段正文甲", "blocks": [self.book["blocks"][0]]}
        review = {"evidence": [str(self.evidence)], "verified": False}
        with self.assertRaisesRegex(ValueError, "尚未标记人工核验"):
            review_batch.compile_batch(self.manifest([reg], [review]), ready=True)
        with self.assertRaisesRegex(ValueError, "区域重叠"):
            review_batch.compile_batch(self.manifest([reg, reg], [review, review]))

    def test_stale_plan_refuses_write(self):
        reg = {"page": 1, "start_prefix": "一段正文甲", "last_prefix": "一段正文甲", "blocks": [dict(self.book["blocks"][0], text="一段正文乙。 ")]}
        review = {"evidence": [str(self.evidence)], "verified": True, "deletion_explanation": "核对原文"}
        merged, _ = review_batch.compile_batch(self.manifest([reg], [review]), ready=True)
        ready = self.root / "ready.json"
        ready.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        self.book["blocks"][0]["text"] = "之后的变更。"
        self.write_book()
        before = self.bookfile.read_bytes()
        proc = run_cli(page_surgery.__file__, str(ready))
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("书稿已变化", proc.stdout + proc.stderr)
        self.assertEqual(self.bookfile.read_bytes(), before)

    def test_backups_do_not_overwrite_with_same_timestamp(self):
        reg = {"page": 1, "start_prefix": "一段正文甲", "last_prefix": "一段正文甲", "blocks": [self.book["blocks"][0]]}
        plan = self.root / "apply.json"
        plan.write_text(json.dumps({"book_dir": str(self.bookdir), "apply": True, "regions": [reg]}, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(page_surgery.time, "strftime", return_value="20260101_010101"), mock.patch.object(sys, "argv", ["page_surgery.py", str(plan)]):
            self.assertEqual(page_surgery.main(), 0)
            self.assertEqual(page_surgery.main(), 0)
        self.assertEqual(len(list(self.bookdir.glob("book.json.bak_*"))), 2)

    def test_cli_requires_matching_dry_plan_for_ready(self):
        reg = {"page": 1, "start_prefix": "一段正文甲", "last_prefix": "一段正文甲", "blocks": [self.book["blocks"][0]]}
        review = {"evidence": [str(self.evidence)], "verified": True}
        manifest = self.manifest([reg], [review])
        cli = [review_batch.__file__, str(manifest)]
        out = self.root / "out"
        missing = run_cli(*cli, "--out", str(out), "--ready")
        self.assertNotEqual(missing.returncode, 0)
        dry = run_cli(*cli, "--out", str(out))
        self.assertEqual(dry.returncode, 0, dry.stderr)
        dry_file = out / "批量试算计划.json"
        ready = run_cli(*cli, "--out", str(out), "--ready", "--dry-plan", str(dry_file))
        self.assertEqual(ready.returncode, 0, ready.stderr)
        self.assertTrue(json.loads((out / "已核批量计划.json").read_text(encoding="utf-8"))["apply"])

    def test_compiled_batch_applies_only_to_disposable_book(self):
        first = {"page": 1, "start_prefix": "一段正文甲", "last_prefix": "一段正文甲", "blocks": [dict(self.book["blocks"][0], text="一段正文甲乙。")]}
        second = {"page": 2, "start_prefix": "表一", "last_prefix": "表一", "blocks": [dict(self.book["blocks"][1], rows=[["原有内容", "保留新增"]])]}
        reviews = [{"evidence": [str(self.evidence)], "verified": True} for _ in range(2)]
        merged, report = review_batch.compile_batch(self.manifest([first, second], reviews), ready=True)
        self.assertEqual(report["region_count"], 2)
        ready = self.root / "ready.json"
        ready.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        proc = run_cli(page_surgery.__file__, str(ready))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        after = json.loads(self.bookfile.read_text(encoding="utf-8"))
        self.assertEqual(after["blocks"][0]["text"], "一段正文甲乙。")
        self.assertEqual(after["blocks"][1]["rows"], [["原有内容", "保留新增"]])
        self.assertEqual(len(list(self.bookdir.glob("book.json.bak_*"))), 1)


if __name__ == "__main__":
    unittest.main()
