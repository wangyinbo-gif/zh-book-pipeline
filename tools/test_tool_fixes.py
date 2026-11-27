# -*- coding: utf-8 -*-
"""tools/test_tool_fixes.py — 方案 v2 · P0-1 四条工具缺陷的回归用例（B1/B4/B5/B7）。

全部用临时目录夹具：不碰 books\，不跑 OCR，不写真实书稿。
运行：python -m unittest tools.test_tool_fixes   或   python tools/test_tool_fixes.py
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pipeline as pl               # noqa: E402
import repair_lost_lines as rl      # noqa: E402
import realign_chapters as rc       # noqa: E402
import fix_unpaired_captions as fc  # noqa: E402


def w(path, text, newline="\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text.replace("\n", newline))


def wj(path, obj, indent=2, newline="\n"):
    w(path, json.dumps(obj, ensure_ascii=False, indent=indent), newline=newline)


def rj(path):
    with io.open(path, encoding="utf-8") as f:
        return json.load(f)


def rb(path):
    with open(path, "rb") as f:
        return f.read()


def sha(path):
    return hashlib.sha256(rb(path)).hexdigest()


def para(t, page=1, y=0, col=0):
    return {"type": "paragraph", "text": t, "page": page, "col": col, "y": y}


def head(t, page=1, y=0, col=0):
    return {"type": "heading", "text": t, "page": page, "col": col, "y": y}


def table(cap, rows, page=1, y=0):
    return {"type": "table", "caption": cap, "rows": rows, "page": page, "col": 0, "y": y}


def make_book(blocks, chapters=None, stats=None):
    b = {"bookTitle": "夹具书", "blocks": blocks,
         "chapters": chapters if chapters is not None else [],
         "stats": stats or {}}
    b["stats"].setdefault("chars", pl.count_chars(b))
    b["stats"].setdefault("pages", 10)
    b["stats"].setdefault("paragraphs", sum(1 for x in blocks if x.get("type") == "paragraph"))
    b["stats"].setdefault("figures", sum(1 for x in blocks if x.get("type") == "figure"))
    b["stats"].setdefault("paired", sum(1 for x in blocks if x.get("type") == "figure" and (x.get("caption") or "").strip()))
    b["stats"].setdefault("chapters", len(b["chapters"]))
    return b


class StageCmdBackwardCompat(unittest.TestCase):
    """B5 的配套：captions 阶段默认 dry-run 之后，流水线的 captions 阶段必须显式 --apply，
    否则转换流程会静默少一步补图注（向后兼容红线）。"""

    def test_captions_stage_passes_apply(self):
        import pipeline as pl
        cmd = pl.stage_cmd("captions", "D:/某书")
        self.assertIn("fix_unpaired_captions.py", os.path.basename(cmd[1]))
        self.assertEqual(cmd[-1], "--apply")

    def test_other_stages_unchanged(self):
        import pipeline as pl
        self.assertEqual(pl.stage_cmd("bookfix", "D:/某书")[2], os.path.join("D:/某书", "book.json"))
        self.assertEqual(len(pl.stage_cmd("toc", "D:/某书")), 3)
        self.assertNotIn("--apply", pl.stage_cmd("readability", "D:/某书"))
        self.assertEqual(len(pl.STAGE_SCRIPTS), len(pl.POST_STAGES) - 1,
                         "preview 走内联代码，其余阶段都要在表里")


class TempDirMixin(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="toolfix_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.book_dir = os.path.join(self.dir, "books", "夹具书")
        os.makedirs(self.book_dir, exist_ok=True)


# --------------------------------------------------------------------------- B1
class B1CharsScope(TempDirMixin):
    def test_chars_of_uses_count_chars_scope(self):
        blocks = [para("正" * 50), head("第一章标题"), table("表1.1 指标", [["甲", "乙"], ["丙", "丁"]])]
        self.assertEqual(rl.chars_of(blocks), pl.count_chars({"blocks": blocks}))
        old_scope = sum(len(b.get("text") or "") for b in blocks if b.get("type") == "paragraph")
        self.assertLess(old_scope, rl.chars_of(blocks), "夹具必须含 heading/table，否则测不出口径差")

    def test_apply_plan_writes_count_chars_scope(self):
        blocks = [para("第一段正文" * 8, page=1, y=100), head("1.1 小节", page=1, y=400),
                  table("表1.1 指标", [["数值" * 10]], page=1, y=500)]
        book = make_book([dict(b) for b in blocks])
        wj(os.path.join(self.book_dir, "book.json"), book)
        plans = [{"op": "append", "idx": 0, "page": 1, "col": 0, "y": 200, "text": "补回的一行正文"}]
        before = book["stats"]["chars"]          # apply_plan 会原地改 book，先存住
        r = rl.apply_plan(self.book_dir, book, blocks, plans)
        out = rj(os.path.join(self.book_dir, "book.json"))
        self.assertEqual(out["stats"]["chars"], pl.count_chars(out))
        self.assertEqual(out["stats"]["chars"], before + len("补回的一行正文"))
        self.assertGreater(pl.count_chars(out),
                           sum(len(b.get("text") or "") for b in out["blocks"] if b.get("type") == "paragraph"),
                           "heading/table 必须计入，否则就是 B1")
        self.assertEqual(out["stats"]["paragraphs"], 1)

    def test_conservation_assertion_same_scope(self):
        blocks = [para("正文" * 30), head("标题")]
        book = make_book([dict(b) for b in blocks])
        wj(os.path.join(self.book_dir, "book.json"), book)
        before = sha(os.path.join(self.book_dir, "book.json"))
        plans = [{"op": "splice", "idx": 0, "a": 3, "page": 1, "col": 0, "y": 10, "text": "插入"}]
        r = rl.apply_plan(self.book_dir, book, blocks, plans)   # 不抛错即守恒成立
        self.assertEqual(r["chars"][1] - r["chars"][0], 2)
        self.assertNotEqual(before, sha(os.path.join(self.book_dir, "book.json")))


# --------------------------------------------------------------------------- B4
class B4MergeAndOrder(TempDirMixin):
    def test_same_region_prepends_merge_in_reading_order(self):
        blocks = [para("原有段落内容", page=171, y=900)]
        plans = [
            {"op": "prepend", "idx": 0, "page": 171, "col": 0, "y": 300, "text": "第二行片段"},
            {"op": "prepend", "idx": 0, "page": 171, "col": 0, "y": 100, "text": "第一行片段"},
            {"op": "prepend", "idx": 0, "page": 171, "col": 0, "y": 200, "text": "第一行片段"},  # 重复
        ]
        merged, held = rl.merge_plans(plans, blocks)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["text"], "第一行片段第二行片段")
        self.assertEqual(held, [])

    def test_cross_page_glue_is_held(self):
        """某书实测反例：p173 的标签不许粘到 p171 的段落。"""
        blocks = [para("p171 的段落", page=171, y=100)]
        plans = [{"op": "append", "idx": 0, "page": 173, "col": 0, "y": 1121, "text": "梁与其他构件的接合，如上："},
                 {"op": "append", "idx": 0, "page": 173, "col": 0, "y": 1308, "text": "斜撑梁与地面的接合，如上："}]
        merged, held = rl.merge_plans(plans, blocks)
        self.assertEqual(merged, [])
        self.assertEqual(len(held), 1)
        self.assertIn("跨页粘连", held[0]["reason"])

    def test_conflicting_ops_on_same_block_are_held(self):
        blocks = [para("段落", page=5, y=100)]
        plans = [{"op": "append", "idx": 0, "page": 5, "col": 0, "y": 120, "text": "A"},
                 {"op": "splice", "idx": 0, "a": 1, "page": 5, "col": 0, "y": 130, "text": "B"}]
        merged, held = rl.merge_plans(plans, blocks)
        self.assertEqual(merged, [])
        self.assertIn("冲突", held[0]["reason"])

    def test_fragment_already_in_block_is_dropped(self):
        blocks = [para("这里已经有一整行正文内容了", page=3, y=10)]
        plans = [{"op": "append", "idx": 0, "page": 3, "col": 0, "y": 20, "text": "一整行正文内容"}]
        merged, held = rl.merge_plans(plans, blocks)
        self.assertEqual(merged, [])

    def test_inserts_stay_separate_blocks_in_reading_order(self):
        blocks = [para("甲段", page=1, y=100), para("乙段", page=2, y=100)]
        plans = [{"op": "insert", "idx": 1, "page": 2, "col": 0, "y": 300, "text": "第二段第二行"},
                 {"op": "insert", "idx": 1, "page": 2, "col": 0, "y": 150, "text": "第二段第一行"}]
        merged, held = rl.merge_plans(plans, blocks)
        self.assertEqual([m["text"] for m in merged], ["第二段第一行", "第二段第二行"], "insert 不许粘成一块")

    def test_apply_plan_single_pass_inserts_keep_order(self):
        blocks = [para("甲段", page=1, y=100), para("乙段", page=2, y=100), para("丙段", page=3, y=100)]
        book = make_book([dict(b) for b in blocks],
                         chapters=[{"title": "第1章", "startBlock": 2, "endBlock": 2, "blocks": [], "chars": 0, "figures": 0}])
        wj(os.path.join(self.book_dir, "book.json"), book)
        plans = [
            {"op": "insert", "idx": 2, "page": 2, "col": 0, "y": 300, "text": "插在后"},
            {"op": "insert", "idx": 2, "page": 2, "col": 0, "y": 150, "text": "插在前"},
        ]
        rl.apply_plan(self.book_dir, book, blocks, plans)
        out = rj(os.path.join(self.book_dir, "book.json"))
        texts = [b["text"] for b in out["blocks"]]
        self.assertEqual(texts, ["甲段", "乙段", "插在前", "插在后", "丙段"])
        self.assertEqual(out["chapters"][0]["startBlock"], 4, "章起点应随插入块数右移")

    def test_apply_plan_holds_cross_page_and_writes_nothing(self):
        blocks = [para("p171 段落", page=171, y=100)]
        book = make_book([dict(b) for b in blocks])
        p = os.path.join(self.book_dir, "book.json")
        wj(p, book)
        before = sha(p)
        plans = [{"op": "append", "idx": 0, "page": 173, "col": 0, "y": 1121, "text": "跨页标签若干字"}]
        r = rl.apply_plan(self.book_dir, book, blocks, plans)
        self.assertEqual(r["merged"], 0)
        self.assertEqual(len(r["held"]), 1)
        self.assertEqual(r["chars"][0], r["chars"][1])
        self.assertEqual(before, sha(p), "挂起的计划不该改动内容")


# --------------------------------------------------------------------------- B5
class B5CacheSourceAndDryRun(TempDirMixin):
    def _mk_pages(self, variants):
        pd = os.path.join(self.book_dir, "pages")
        os.makedirs(pd, exist_ok=True)
        for pg, kinds in variants.items():
            for kind, n_lines in kinds.items():
                wj(os.path.join(pd, "page_%03d_%s.json" % (pg, kind)),
                   [{"text": "行%d" % i, "x0": 100, "y0": 100 + i * 10} for i in range(n_lines)])

    def test_ocr2_wins_and_is_the_only_source(self):
        self._mk_pages({1: {"ocr": 93, "ocr2": 30},
                        2: {"ocr": 12},
                        3: {"text": 40, "ocr2": 7}})
        pages, kinds = fc.load_pages(self.book_dir)
        self.assertEqual(kinds[1], "ocr2")
        self.assertEqual(len(pages[1]), 30, "混读时会被 93 行的 _ocr 覆盖")
        self.assertEqual(kinds[2], "ocr")
        self.assertEqual(len(pages[2]), 12)
        self.assertEqual(kinds.get(3), "ocr2")
        self.assertEqual(len(pages[3]), 7, "_text 缓存不得参与")

    def test_dry_run_does_not_write(self):
        blocks = [{"type": "figure", "page": 1, "y": 110, "id": "fig-001", "caption": ""},
                  para("正文若干字" * 6, page=1, y=400)]
        book = make_book([dict(b) for b in blocks])
        p = os.path.join(self.book_dir, "book.json")
        wj(p, book, indent=2, newline="\n")
        self._mk_pages({1: {"ocr2": 3}})
        wj(os.path.join(self.book_dir, "pages", "page_001_ocr2.json"),
           [{"text": "图1.1 示例图注", "x0": 100, "y0": 120},
            {"text": "无关行", "x0": 100, "y0": 300},
            {"text": "还有一行", "x0": 100, "y0": 320}])
        before = sha(p)
        r = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "fix_unpaired_captions.py"),
                            self.book_dir], capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("dry-run", r.stdout)
        self.assertIn("计划改动 1 条", r.stdout)
        self.assertEqual(before, sha(p), "默认预演不该写盘")

    def test_apply_writes_and_keeps_format(self):
        blocks = [{"type": "figure", "page": 1, "y": 110, "id": "fig-001", "caption": ""},
                  para("正文若干字" * 6, page=1, y=400)]
        book = make_book([dict(b) for b in blocks])
        p = os.path.join(self.book_dir, "book.json")
        wj(p, book, indent=1, newline="\r\n")
        self._mk_pages({1: {"ocr2": 1}})
        wj(os.path.join(self.book_dir, "pages", "page_001_ocr2.json"),
           [{"text": "图1.1 示例图注", "x0": 100, "y0": 120}])
        raw = rb(p)
        r = subprocess.run([sys.executable, "-X", "utf8", os.path.join(HERE, "fix_unpaired_captions.py"),
                            self.book_dir, "--apply"], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        after = rb(p)
        self.assertNotEqual(raw, after)
        fmt_b, fmt_a = fc.detect_json_format(raw), fc.detect_json_format(after)
        self.assertEqual((fmt_b["newline"], fmt_b["indent"]), (fmt_a["newline"], fmt_a["indent"]),
                         "写回漂移了换行/缩进")
        out = rj(p)
        self.assertEqual(out["blocks"][0]["caption"], "图1.1 示例图注")
        baks = [x for x in os.listdir(self.book_dir) if "图注补全前" in x]
        self.assertEqual(len(baks), 1)


# --------------------------------------------------------------------------- B7
class B7FormatPreserve(TempDirMixin):
    def _fixture(self, indent, newline):
        blocks = [head("第1章 甲", page=1, y=10), para("正文" * 40, page=1, y=50)]
        ch = [{"id": "ch-1", "title": "第1章 甲", "startBlock": 0, "endBlock": 1,
               "blocks": [blocks[0]], "chars": 10, "figures": 0}]
        book = make_book(blocks, chapters=ch)
        p = os.path.join(self.book_dir, "book.json")
        wj(p, book, indent=indent, newline=newline)
        return p, book

    def test_detect_reads_bytes_not_text(self):
        p, _ = self._fixture(2, "\n")
        raw = rb(p)
        fmt = rc.detect_json_format(raw)
        self.assertEqual(fmt["newline"], "\n")
        self.assertEqual(fmt["indent"], 2)
        self.assertEqual(fmt["crlf"], 0)
        p2 = os.path.join(self.dir, "compact.json")
        w(p2, json.dumps({"a": 1, "b": [1, 2]}))
        self.assertTrue(rc.detect_json_format(rb(p2))["compact"])

    def test_realign_keeps_lf_indent2(self):
        p, book = self._fixture(2, "\n")
        before = rb(p)
        fb = rc.detect_json_format(before)
        rc.realign(self.book_dir)
        after = rb(p)
        fa = rc.detect_json_format(after)
        self.assertEqual((fa["newline"], fa["indent"], fa["compact"]),
                         (fb["newline"], fb["indent"], fb["compact"]))
        self.assertGreater(after.count(b"\n"), 0)
        self.assertEqual(after.count(b"\r\n"), 0, "LF 书稿被写成 CRLF 就是 B7")
        out = json.loads(after.decode("utf-8"))
        self.assertEqual(out["chapters"][0]["blocks"], out["blocks"][0:2])

    def test_realign_keeps_crlf_indent1(self):
        p, book = self._fixture(1, "\r\n")
        fb = rc.detect_json_format(rb(p))
        rc.realign(self.book_dir)
        fa = rc.detect_json_format(rb(p))
        self.assertEqual((fa["newline"], fa["indent"]), ("\r\n", 1))
        self.assertGreater(fa["crlf"], 0)

    def test_dump_roundtrip_semantic_equal(self):
        obj = {"a": [1, 2], "b": {"c": "中文"}}
        for fmt in ({"newline": "\n", "indent": 2, "compact": False},
                   {"newline": "\r\n", "indent": 1, "compact": False},
                   {"newline": "\n", "indent": None, "compact": True}):
            s = rc.dump_json_preserving(obj, fmt)
            self.assertEqual(json.loads(s), obj)
            if fmt["newline"] == "\r\n":
                self.assertIn("\r\n", s)
            else:
                self.assertNotIn("\r\n", s)


if __name__ == "__main__":
    unittest.main(verbosity=2)
