# -*- coding: utf-8 -*-
"""tools/test_run_info.py — run_info 模块单元测试（P1-02 附带；零第三方依赖，CI 可跑）。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_info  # noqa: E402


class TestRunInfo(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="runinfo_")
        self.book = os.path.join(self.dir, "book")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_write_creates_valid_json(self):
        src = os.path.join(self.dir, "in.pdf")
        with open(src, "wb") as f:
            f.write(b"%PDF-1.4 fake\n" * 10)
        p = run_info.write_run_info(self.book, "convert",
                                    {"input": src, "params": {"scale": 3.0}})
        self.assertTrue(os.path.exists(p))
        d = json.load(open(p, encoding="utf-8"))
        self.assertEqual(d["version"], 1)
        e = d["entries"][-1]
        self.assertEqual(e["stage"], "convert")
        self.assertEqual(e["params"]["scale"], 3.0)
        import hashlib
        want = hashlib.sha256(open(src, "rb").read()).hexdigest()
        self.assertEqual(e["input"]["sha256"], want)
        self.assertEqual(e["input"]["bytes"], os.path.getsize(src))
        self.assertTrue(e["python"])
        self.assertIn("pillow", e["deps"])
        self.assertTrue(e["os"]["platform"])

    def test_append_and_trim(self):
        for i in range(run_info.MAX_ENTRIES + 5):
            run_info.write_run_info(self.book, "s%d" % i, {"params": {"i": i}})
        d = json.load(open(os.path.join(self.book, "run_info.json"), encoding="utf-8"))
        self.assertEqual(len(d["entries"]), run_info.MAX_ENTRIES)
        self.assertEqual(d["entries"][-1]["params"]["i"], run_info.MAX_ENTRIES + 4)
        self.assertEqual(d["last_stage"], "s%d" % (run_info.MAX_ENTRIES + 4))

    def test_corrupt_file_replaced(self):
        os.makedirs(self.book, exist_ok=True)
        bad = os.path.join(self.book, "run_info.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        run_info.write_run_info(self.book, "post", {})
        d = json.load(open(bad, encoding="utf-8"))
        self.assertEqual(d["entries"][0]["stage"], "post")

    def test_missing_input_degrades(self):
        p = run_info.write_run_info(self.book, "convert", {"input": self.dir + "/nope.pdf"})
        e = json.load(open(p, encoding="utf-8"))["entries"][-1]
        self.assertIsNone(e["input"]["sha256"])

    def test_git_fields_present(self):
        p = run_info.write_run_info(self.book, "x", {"root": self.dir})
        e = json.load(open(p, encoding="utf-8"))["entries"][-1]
        for k in ("commit", "branch", "dirty", "root"):
            self.assertIn(k, e["git"])

    def test_cli(self):
        import subprocess
        src = os.path.join(self.dir, "a.pdf")
        open(src, "wb").write(b"x" * 32)
        r = subprocess.run([sys.executable, os.path.join(run_info.HERE, "run_info.py"),
                            self.book, "--stage", "post", "--input", src,
                            "--param", "scale=3.5", "--param", "mode=ocr"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stderr)
        d = json.load(open(os.path.join(self.book, "run_info.json"), encoding="utf-8"))
        e = d["entries"][-1]
        self.assertEqual(e["params"], {"scale": 3.5, "mode": "ocr"})


if __name__ == "__main__":
    unittest.main()
