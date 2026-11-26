# -*- coding: utf-8 -*-
"""tools/test_e2e_check_units.py — e2e_check 纯函数单元测试（不跑 OCR，CI 可跑）。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import e2e_check as E  # noqa: E402


def mk_book(pages=2, per_page=None, chapters=2, chars=500):
    blocks = []
    for p, texts in (per_page or {1: ["春山杂咏石径萦回"], 2: ["读书杂记贵在有恒"]}).items():
        for i, t in enumerate(texts):
            blocks.append({"id": "b%d_%d" % (p, i), "type": "paragraph",
                           "text": t, "page": p})
    return {"title": "测试", "pages": pages, "chapters": [],
            "stats": {"pages": pages, "chars": chars, "paragraphs": len(blocks),
                      "chapters": chapters, "figures": 0},
            "blocks": blocks}


def mk_expected(**kw):
    exp = {"fixture": "x.pdf", "pages": 2, "chapters_min": 1, "chars_min_total": 100,
           "structure_keys": ["title", "blocks", "pages", "stats", "chapters"],
           "must_contain": {"1": ["春山杂咏"], "2": ["贵在有恒"]},
           "cer": {"tolerance": 0.35, "refs": {"1": "春山杂咏石径萦回",
                                              "2": "读书杂记贵在有恒"}}}
    exp.update(kw)
    return exp


class TestHelpers(unittest.TestCase):
    def test_norm_cjk(self):
        self.assertEqual(E.norm_cjk("春山，杂咏 CI 2026·稿"), "春山杂咏稿")

    def test_cer_identical_zero(self):
        self.assertEqual(E.cer_of("石径萦回入翠微", "石径萦回入翠微"), 0.0)

    def test_cer_noisy_under_tolerance(self):
        ref = "夜半春声响渐密晓看庭树绿初匀农家知是田水足平明荷锄问前津"
        got = "夜半春声响渐密晓看庭树绿初匀农家知是田水足平明荷锄问前斤"
        self.assertLess(E.cer_of(ref, got), 0.35)

    def test_cer_garbage_over_tolerance(self):
        self.assertGreater(E.cer_of("石径萦回入翠微白云深处有人归", "之乎者也矣焉哉夫然则"), 0.35)

    def test_cer_empty_ref(self):
        self.assertEqual(E.cer_of("", "任意"), 0.0)


class TestStructure(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e2eu_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _write(self, book):
        with open(os.path.join(self.dir, "book.json"), "w", encoding="utf-8") as f:
            json.dump(book, f, ensure_ascii=False)
        return self.dir

    def test_pass(self):
        rep = {}
        fails = E.check_structure(self._write(mk_book()), mk_expected(), rep)
        self.assertEqual(fails, [], rep)
        self.assertTrue(all(rep["must_contain"].values()))
        self.assertEqual(rep["cer"]["p1"], 0.0)

    def test_pages_mismatch(self):
        book = mk_book(pages=3)
        book["stats"]["pages"] = 3
        fails = E.check_structure(self._write(book), mk_expected(pages=2), {})
        self.assertTrue(any("页数" in f for f in fails))

    def test_string_miss(self):
        book = mk_book(per_page={1: ["春山杂咏石径萦回"], 2: ["完全不同的文字"]})
        fails = E.check_structure(self._write(book), mk_expected(), {})
        self.assertTrue(any("字串未命中" in f for f in fails))

    def test_cer_fail_flagged(self):
        book = mk_book(per_page={1: ["驴唇不对马嘴"], 2: ["读书杂记贵在有恒"]})
        fails = E.check_structure(self._write(book), mk_expected(), {})
        self.assertTrue(any("CER" in f for f in fails))

    def test_no_page_blocks_fallback(self):
        book = mk_book()
        for b in book["blocks"]:
            b.pop("page")
        fails = E.check_structure(self._write(book), mk_expected(), {})
        self.assertEqual(fails, [])  # 全文兜底仍应命中

    def test_schema_ok_and_bad(self):
        exp = mk_expected(structure_keys=["title", "blocks", "stats", "chapters"])
        self.assertEqual(E.check_schema(self._write(mk_book()), exp, {}), [])
        book = mk_book()
        del book["chapters"]
        book["pages"] = 99  # 与 stats.pages 不一致
        book["blocks"].append({"id": "b1_0", "text": "重复id"})
        book["blocks"].append({"text": "无type块", "page": 1})
        book["blocks"].append({"type": "paragraph", "text": "越界", "page": 99})
        fails = E.check_schema(self._write(book), exp, {})
        self.assertTrue(any("结构键" in f for f in fails))
        self.assertTrue(any("pages(99)" in f for f in fails))
        self.assertTrue(any("type" in f for f in fails))
        self.assertTrue(any("越界" in f for f in fails))


class TestLocateAndModels(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="e2ul_")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_locate_normal(self):
        d = os.path.join(self.dir, "横排测试")
        os.makedirs(d)
        open(os.path.join(d, "book.json"), "w").write("{}")
        self.assertEqual(E.locate_book(self.dir, "横排测试.pdf"), d)

    def test_locate_quarantined(self):
        d = os.path.join(self.dir, "需人工处理", "未达标书稿", "横排测试")
        os.makedirs(d)
        open(os.path.join(d, "book.json"), "w").write("{}")
        self.assertEqual(E.locate_book(self.dir, "横排测试.pdf"), d)

    def test_verify_models_no_manifest_skips(self):
        ok, rows = E.verify_models(verbose=False)
        self.assertIsInstance(ok, bool)  # 无清单时 True（warn 跳过），有清单时也不崩


if __name__ == "__main__":
    unittest.main()
