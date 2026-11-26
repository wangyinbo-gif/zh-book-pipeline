# -*- coding: utf-8 -*-
"""tools/vertical_ocr.py + tools/vertical_book.py 的单元测试与 S1 基线复现（回归 c）

用法：
  python -X utf8 tools/test_vertical_ocr.py            # 纯函数单测（不加载模型，秒级）
  python -X utf8 tools/test_vertical_ocr.py --cer      # 另跑 S1 两页 CER 复现（需模型+参考稿）

CER 复现基线（S1 固化值，2026-11-25 23:10）：
  p41 原始 1.82% / 异体归一 0.40% / 标点偏差 0.0% / 杂符 0
  p121 原始 2.97% / 异体归一 1.91% / 标点偏差 0.0% / 杂符 0
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

TOOLS = Path(__file__).resolve().parent
sys.path.insert(0, str(TOOLS))

import vertical_book as VB  # noqa: E402
import vertical_ocr as VO  # noqa: E402

# CER 复现（--cer）需自备扫描 PDF 与人工定稿参考稿：用环境变量 VERTICAL_EVAL_PDF / VERTICAL_EVAL_REF 指定，留空则打印 SKIP。
DEFAULT_PDF = ""
DEFAULT_REF = ""
S1_BASE = {41: {"cer_raw": 0.0182, "cer_norm": 0.0040, "punct_dev": 0.0},
           121: {"cer_raw": 0.0297, "cer_norm": 0.0191, "punct_dev": 0.0}}


# ---------------------------------------------------------------- 假图

def fake_page(cols=((20, 32), (72, 84), (124, 136)), H=300, W=200, glyph_rows=8) -> np.ndarray:
    """白底 + 若干竖列 + 列内等距墨块，用来测列槽/切字。"""
    img = np.full((H, W), 255, dtype=np.uint8)
    for x0, x1 in cols:
        for i in range(glyph_rows):
            y0 = 20 + i * ((H - 40) // max(1, glyph_rows))
            img[y0:y0 + 24, x0:x1] = 30
    return img


class OCRPrimitives(unittest.TestCase):
    def test_column_slots_pitch(self):
        slots, pitch = VO.column_slots(fake_page())
        self.assertGreaterEqual(len(slots), 3)
        self.assertTrue(45 <= pitch <= 60, "pitch=%d" % pitch)

    def test_column_slots_blank_page(self):
        slots, pitch = VO.column_slots(np.full((300, 200), 255, dtype=np.uint8))
        self.assertEqual(slots, [])
        self.assertGreaterEqual(pitch, 18)

    def test_column_slots_ignores_saturated_edge(self):
        """Lead 修的崩溃 bug ①：整列饱和黑边/装订线不得带偏列距。"""
        img = fake_page()
        img[:, :6] = 0                       # 整列饱和黑边
        slots, pitch = VO.column_slots(img)
        self.assertGreaterEqual(len(slots), 3)
        self.assertTrue(45 <= pitch <= 60, "pitch=%d" % pitch)

    def test_column_slots_no_argmin_crash(self):
        """Lead 修的崩溃 bug ②：极窄/空页窗口越界不得抛 ValueError。"""
        for W in (10, 40, 80):
            gray = np.full((200, W), 255, dtype=np.uint8)
            gray[50:150, max(0, W // 2 - 2):W // 2 + 2] = 0
            VO.column_slots(gray)            # 不抛即过

    def test_glyph_boxes(self):
        col = fake_page(cols=((10, 30),))[:, 10:30]
        boxes = VO.glyph_boxes(col)
        self.assertGreaterEqual(len(boxes), 6)

    def test_fix_chars(self):
        metas = [{"bands": 1, "h": 4, "w": 20, "gw": 20},
                 {"bands": 2, "h": 30, "w": 20, "gw": 20},
                 {"bands": 1, "h": 30, "w": 20, "gw": 20}]
        out, n = VO._fix_chars("一_三", metas)
        self.assertEqual(out, "一二三")
        self.assertEqual(n, 2)

    def test_normalize_punct(self):
        self.assertEqual(VO.normalize_punct("’°“”"), "、。「」")

    def test_classify(self):
        self.assertEqual(VO.classify("卷之一城北内外"), "卷标")
        self.assertEqual(VO.classify("一二三四"), "页码")
        self.assertEqual(VO.classify("某城风物志", ears=["某城风物志"]), "书耳")
        self.assertEqual(VO.classify("北窗雨後蔓清圓。"), "正文")
        self.assertEqual(VO.classify("  "), "空")

    def test_basic_flags(self):
        d = {"han": 0, "punct": 0, "slots": 0, "pitch": 0, "bad": 0, "suspect": 0,
             "col_errors": [], "h_small_share": 0.0}
        self.assertIn("零字", VO.basic_flags(d))
        d2 = dict(d, han=400, punct=0, slots=16, pitch=52)
        self.assertIn("无句读", VO.basic_flags(d2))
        self.assertNotIn("零字", VO.basic_flags(d2))
        d3 = dict(d2, punct=60, bad=2)
        self.assertIn("杂符", VO.basic_flags(d3))

    def test_enrich_flags(self):
        recs = [{"diag": {"han": h, "slots": 16, "pitch": 52, "resplit": 0, "flags": []}}
                for h in (400, 420, 380, 100)]
        VO.enrich_flags(recs)
        self.assertTrue(any("低字" in r["diag"]["flags"] for r in recs))

    def test_cache_key(self):
        k1 = VO.cache_key("a" * 40, 41, 3.0, "v1")
        self.assertEqual(k1, VO.cache_key("a" * 40, 41, 3.0, "v1"))
        self.assertNotEqual(k1, VO.cache_key("a" * 40, 42, 3.0, "v1"))
        self.assertNotEqual(k1, VO.cache_key("a" * 40, 41, 3.0, "v2"))
        self.assertNotEqual(k1, VO.cache_key("a" * 40, 41, 4.0, "v1"))


class BookAssembly(unittest.TestCase):
    def test_cn_numbers(self):
        self.assertEqual(VB.parse_volume_head("卷之一城北内外一一一"), (1, "城北内外"))
        self.assertEqual(VB.parse_volume_head("卷之二城南内外一"), (2, "城南内外"))
        self.assertIsNone(VB.parse_volume_head("卷之城北内外七"))   # 版心误读，不予采信
        self.assertEqual(VB.cn_digits_to_int("一四"), 14)
        self.assertEqual(VB.cn_digits_to_int("二五"), 25)
        self.assertEqual(VB.cn_digits_to_int("十"), 10)
        self.assertIsNone(VB.cn_digits_to_int("十十十十"))
        self.assertEqual(VB.int_to_cn(8), "八")

    def test_strip_running_head(self):
        cores = ["风物志", "物志"]
        out, num, hit = VB.strip_running_head("某城风物志四必繇太學", cores, [])
        self.assertTrue(hit)
        self.assertEqual(num, 4)
        self.assertTrue(out.startswith("必繇太學"), out)
        out, num, hit = VB.strip_running_head("某城风物志一八賢功", cores, [])
        self.assertEqual(num, 18)
        self.assertTrue(out.startswith("賢功"), out)
        # 正文里的普通句子不得被误吃
        out, num, hit = VB.strip_running_head("之。言鼓者，表厥攸始也", cores, [])
        self.assertFalse(hit)
        self.assertEqual(out, "之。言鼓者，表厥攸始也")
        # 已知印刷页码时精确剔页码，不能把「十刹海」的「十」当页码
        out, num, hit = VB.strip_running_head("某城风物志卷之一二十刹海千佛寺", cores,
                                              ["城北内外"], expect_num=2)
        self.assertTrue(hit)
        self.assertEqual(num, 2)
        self.assertTrue(out.startswith("十刹海"), out)

    def test_strip_running_foot(self):
        self.assertEqual(VB.strip_running_foot("太學石鼓文丞相祠某城风物志卷之一二",
                                               ["某城风物志", "风物志"]),
                         "太學石鼓文丞相祠")
        self.assertEqual(VB.strip_running_foot("豈得空沉淪", ["某城风物志"]), "豈得空沉淪")

    def test_is_toc_page(self):
        self.assertTrue(VB.is_toc_page({"text": "某城风物志總目一太學石鼓"}))
        self.assertFalse(VB.is_toc_page({"text": "太學石鼓文"}))

    def test_page_gate_levels(self):
        def rec(pg, han, punct, flags, err=None):
            return {"page": pg, "text": "", "columns": [],
                    "diag": {"han": han, "punct": punct, "slots": 16, "pitch": 52,
                             "flags": flags, "error": err}}
        recs = [rec(1, 5, 0, ["极低字"]), rec(2, 400, 30, []),
                rec(3, 0, 0, ["零字"]), rec(4, 420, 33, []),
                rec(5, 100, 0, ["低字", "无句读"]), rec(6, 0, 0, ["抛错"], err="X: y")]
        g = VB.page_gate(recs, body_start=2, toc_pages=set(), title_pages=set(),
                         dropped_cover={1})
        sev = {x["page"]: x["severity"] for x in g["issues"]}
        self.assertEqual(sev[1], "参考")       # 前置页
        self.assertEqual(sev[3], "参考")       # 前后有正文的空白页
        self.assertEqual(sev[5], "硬")         # 正文低字+无句读 → 硬
        self.assertEqual(sev[6], "硬")         # 正文抛错
        self.assertEqual(g["hardPages"], [5, 6])

    def _rec(self, pg, text, heads=(), han=None, punct=None, columns=None):
        return {"page": pg, "text": text, "cached": False,
                "columns": columns or [{"kind": "正文", "x0": 100, "x1": 130, "text": text}],
                "diag": {"han": han if han is not None else VO.cjk_count(text),
                         "punct": punct if punct is not None else VO.count_punct(text),
                         "slots": 16, "pitch": 52, "flags": [], "heads": list(heads),
                         "bad": 0, "suspect": 0, "corrections": 0, "resplit": 0,
                         "glyphs": 0, "h_med": 27, "h_small_share": 0.1,
                         "punct_per100": 10.0, "col_errors": [], "seconds": 0.1}}

    def test_assemble_chapters_and_merge(self):
        long1 = "太學石鼓文，在都城東北艮隅。" * 8
        long2 = "文丞相祠，今順天府學。" * 8
        recs = [
            self._rec(1, "版權頁文字很少。", han=8),          # 封面级 → 剔除
            self._rec(2, "出版說明" + long1, han=60),
            self._rec(3, "某城风物志總目一太學石鼓文丞相祠"),      # 目录 → 剔除
            self._rec(4, "某城风物志卷之五城北內外摩訶菴釣魚臺", heads=["卷之五"]),
            self._rec(5, "某城风物志五" + long1, heads=["卷之五城北內外五"], han=200),
            self._rec(6, "某城风物志六" + long2, han=200),
            self._rec(7, "某城风物志卷之六西城外摩訶菴釣魚臺", heads=["卷之六西城外"]),
            self._rec(8, "某城风物志八" + long1, han=200),
        ]
        book, qc = VB.assemble(recs, 40, "某城风物志", "某城风物志", "x.pdf",
                               {"scale": 3.0, "paramVersion": "t", "headCores": ["风物志"]})
        titles = [c["title"] for c in book["chapters"]]
        self.assertIn("卷一 城北內外", titles)
        self.assertIn("卷二 西城外", titles)
        self.assertGreaterEqual(len(book["chapters"]), 3)
        self.assertEqual(book["stats"]["pages"], 40)
        for k in ("pages", "chars", "paragraphs", "figures", "paired", "chapters"):
            self.assertIn(k, book["stats"])
        self.assertGreater(book["stats"]["chars"], 400)
        self.assertEqual(book["source"], "x.pdf")
        # 跨页合并：正文段落块 pages 字段应出现（4 页连续正文）
        merged = [b for b in book["blocks"] if b.get("type") == "paragraph" and b.get("pages")]
        self.assertTrue(merged, "未发生跨页/跨栏合并")
        # 目录页/封面级页剔除记录
        self.assertEqual(book["_fix"]["removedTocPages"], [3])
        self.assertEqual(book["_fix"]["droppedPages"], [1])
        # 卷首页 heading + 篇目 note
        self.assertTrue(any(b.get("type") == "note" and "摩訶菴" in b.get("text", "")
                            for b in book["blocks"]))
        self.assertEqual(qc["layout"], "vertical")

    def test_assemble_multi_work(self):
        """多部合装：南庵夢記+西樓夢尋 这种一卷一部、部内卷号重启的书，按卷端页定章。"""
        long1 = "鍾山上有雲氣，浮浮冉冉，紅紫間之，人言王氣。" * 6
        long2 = "西湖總記，西湖之景，四時不同，晴雨皆宜。" * 6
        colo1 = "南菴夢記卷一某郡某氏編"
        colo2 = "西樓夢尋卷一某郡某氏著"
        recs = [
            self._rec(1, "整理說明" + long1, han=200),
            self._rec(2, long1, han=200, columns=[
                {"kind": "正文", "x0": 100, "x1": 130, "text": colo1},
                {"kind": "正文", "x0": 200, "x1": 230, "text": long1}]),
            self._rec(3, long1, han=200, columns=[
                {"kind": "正文", "x0": 300, "x1": 330, "text": "南菴夢記四"},
                {"kind": "正文", "x0": 200, "x1": 230, "text": long1}]),
            self._rec(4, long2, han=200, columns=[
                {"kind": "正文", "x0": 100, "x1": 130, "text": colo2},
                {"kind": "正文", "x0": 200, "x1": 230, "text": long2}]),
            self._rec(5, long2, han=200),
        ]
        book, qc = VB.assemble(recs, 5, "南庵梦记 西楼梦寻", "南庵梦记 西楼梦寻", "x.pdf",
                               {"scale": 3.0, "paramVersion": "t",
                                "ears": ["南庵夢記", "西樓夢尋"],
                                "headCores": ["夢記", "夢尋"]})
        titles = [c["title"] for c in book["chapters"]]
        self.assertEqual(qc["works"], ["南庵夢記", "西樓夢尋"])
        self.assertIn("南庵夢記 卷一", titles)
        self.assertIn("西樓夢尋 卷一", titles)
        self.assertEqual([c["startPage"] for c in book["chapters"]], [1, 2, 4])
        # 部之间不重叠：每页只归属一章
        seen = []
        for c in book["chapters"]:
            seen.extend(range(c["startPage"],
                              (book["chapters"][book["chapters"].index(c) + 1]["startPage"]
                               if book["chapters"].index(c) + 1 < len(book["chapters"]) else 6)))
        self.assertEqual(sorted(seen), [1, 2, 3, 4, 5])
        self.assertEqual(book["stats"]["chapters"], 3)

    def test_printed_pages(self):
        recs = [self._rec(23, "x" * 300, heads=["卷之一城北内外三"]),
                self._rec(24, "x" * 300), self._rec(25, "x" * 300, heads=["卷之一城北内外五"]),
                self._rec(26, "x" * 300), self._rec(27, "x" * 300, heads=["卷之一城北内外七"]),
                self._rec(28, "x" * 300, heads=["卷之一城北内外八"])]
        pp = VB.printed_pages(recs)
        self.assertEqual(pp.get(23), 3)
        self.assertEqual(pp.get(28), 8)


# ---------------------------------------------------------------- 页级去倾斜（任务⑤投产）

def ruled_page(n_cols=8, pitch=52, H=600, glyph_rows=12) -> np.ndarray:
    """规整竖栏页：宽 12px 墨列、列距 pitch、列内等距字块 —— 找角/校验用合成图。"""
    W = int(n_cols * pitch + pitch)
    cols = [(20 + i * pitch, 32 + i * pitch) for i in range(n_cols)]
    return fake_page(cols=cols, H=H, W=W, glyph_rows=glyph_rows)


class PageDeskew(unittest.TestCase):
    MED = {"pages": 399, "ok": True, "pitch": 52.0, "slots": 17.0, "han": 405.0}

    def _diag(self, han, pitch, slots, error=None):
        return {"han": han, "pitch": pitch, "slots": slots, "error": error}

    def test_trigger_collapse(self):
        """p57/77/215 型：列距崩 + 栏数翻倍 + 正文页。"""
        self.assertTrue(VO.deskew_trigger(self._diag(706, 18, 40), self.MED))
        self.assertTrue(VO.deskew_trigger(self._diag(657, 18, 40), self.MED))
        self.assertTrue(VO.deskew_trigger(self._diag(732, 18, 44), self.MED))

    def test_trigger_harmonic(self):
        """p264 型：pitch ≈ 2× 中位谐波翻倍。"""
        self.assertTrue(VO.deskew_trigger(self._diag(344, 105, 10), self.MED))

    def test_trigger_body_page_guard(self):
        """正文页前置条件：整页书影页（han 4–53）/封面/目录必须被 A/B 判据排除。"""
        self.assertFalse(VO.deskew_trigger(self._diag(53, 18, 30), self.MED))    # 书影页
        self.assertFalse(VO.deskew_trigger(self._diag(9, 39, 29), self.MED))     # 某城 p1 封面（AB 原判据误触）
        self.assertFalse(VO.deskew_trigger(self._diag(55, 104, 10), self.MED))   # 某城 p209 卷目录（谐波形态、字少）
        self.assertFalse(VO.deskew_trigger(self._diag(0, 129, 0), self.MED))     # 空白页

    def test_trigger_normal_and_partial(self):
        """正常页与半触发页不动：同判据实测 p56 强校也 0 收益，保守不改。"""
        self.assertFalse(VO.deskew_trigger(self._diag(413, 52, 15), self.MED))   # p56 正常
        self.assertFalse(VO.deskew_trigger(self._diag(494, 52, 17), self.MED))   # p41（S1 基线页）
        self.assertFalse(VO.deskew_trigger(self._diag(472, 52, 16), self.MED))   # p121（S1 基线页）
        self.assertFalse(VO.deskew_trigger(self._diag(400, 18, 19), self.MED))   # p115 列距崩但栏数未翻倍
        self.assertFalse(VO.deskew_trigger(self._diag(400, 52, 30), self.MED))   # 栏多但列距正常

    def test_trigger_requires_medians(self):
        d = self._diag(706, 18, 40)
        self.assertFalse(VO.deskew_trigger(d, {}))
        self.assertFalse(VO.deskew_trigger(d, {"ok": False, "pitch": 52, "slots": 17, "han": 400}))
        self.assertFalse(VO.deskew_trigger(d, {"ok": True, "pitch": 0, "slots": 17, "han": 400}))
        self.assertFalse(VO.deskew_trigger({"han": 706, "pitch": 18, "slots": 40, "error": "X: y"}, self.MED))

    def test_skew_medians(self):
        recs = []
        for h, pt, sl in [(400, 52, 17), (420, 52, 16), (380, 53, 18), (0, 0, 0),
                          (706, 18, 40)] * 20:
            recs.append({"diag": self._diag(h, pt, sl)})
        med = VO.skew_medians(recs)
        self.assertEqual(med["pages"], 100)
        self.assertTrue(med["ok"])
        self.assertEqual(med["pitch"], 52)          # 0 值不进样本
        self.assertEqual(med["slots"], 17.5)        # 16/17/18/40 各 20 → 中位 17.5
        self.assertTrue(380 <= med["han"] <= 420, "han=%s" % med["han"])
        small = VO.skew_medians(recs[:10])          # 样本不足 → 不启用
        self.assertFalse(small["ok"])

    def test_estimate_skew_recovers_angle(self):
        """合成页歪 +2.5°，±3°/0.25° 网格应找回 ≈−2.5°。"""
        g = ruled_page()
        skewed = VO.rotate_gray(g, 2.5)
        ang, curve = VO.estimate_skew(skewed)
        self.assertEqual(len(curve), 25)            # -3.0..3.0 步长 0.25
        self.assertTrue(-3.0 <= ang <= -2.0, "ang=%s" % ang)

    def test_estimate_skew_tall_page_recovers(self):
        """高页（H=1400）歪 +2.5°：网格找回 −2.5°，且首峰即真峰（曲线单调回落）。"""
        g = ruled_page(n_cols=8, H=1400)
        sk = VO.rotate_gray(g, 2.5)
        ang, curve = VO.estimate_skew(sk)
        self.assertEqual(ang, -2.5)
        self.assertEqual(VO.skew_peaks(curve)[0][0], -2.5)

    def test_harmonic_fallback_second_peak(self):
        """2× 谐波回退（p247/p347 型）：伪高峰转正后列距仍崩 → 校验拒 → 自动退次峰采用。"""
        med = {"ok": True, "pages": 100, "pitch": 52.0, "slots": 8.0, "han": 400.0}
        # 页被扫描歪了 +2.5°：伪高峰 0.0°（方差假象，实际仍歪）列距仍崩 → 校验拒；
        # 真峰 −2.5° 落回 pitch 52/8 栏 → 采用。等价复现 p247/p347 的「方差骗人、结构兜底」。
        g = VO.rotate_gray(ruled_page(n_cols=8, H=1400), 2.5)
        ang, tried, _ = VO.choose_deskew_angle(g, med, candidates=[(0.0, 9e9), (-2.5, 1e9)])
        self.assertEqual(ang, -2.5)
        self.assertEqual(len(tried), 2)
        self.assertFalse(tried[0]["ok"])            # 假峰：转正后 pitch 21 仍在崩区 → 拒
        self.assertTrue(tried[1]["ok"])             # 次峰结构落回正常带 → 采用

    def test_harmonic_reject_all_keeps_original(self):
        """所有候选峰都过不了校验 → 返回 None（调用方必须维持原结果，不碰基础缓存）。"""
        med = {"ok": True, "pages": 100, "pitch": 52.0, "slots": 8.0, "han": 400.0}
        g = ruled_page(n_cols=8, H=1400)
        ang, tried, _ = VO.choose_deskew_angle(
            g, med, candidates=[(2.5, 9e9), (2.75, 8e9), (-2.75, 7e9)])
        self.assertIsNone(ang)
        self.assertEqual(len(tried), 3)
        self.assertTrue(all(not t["ok"] for t in tried))

    def test_cache_variant_isolation(self):
        """变体子键与基础键分离：基础缓存键逐字节不变，去倾斜结果另存。"""
        base = VO.cache_key("a" * 40, 57, 3.0)
        variant = VO.cache_key("a" * 40, 57, 3.0, VO.PARAM_VERSION + "+" + VO.DESKW_VERSION)
        self.assertNotEqual(base, variant)
        self.assertEqual(VO.PARAM_VERSION, "vertical-ocr-v1")   # 不得整书失效
        self.assertEqual(base, VO.cache_key("a" * 40, 57, 3.0, "vertical-ocr-v1"))


# ---------------------------------------------------------------- S1 基线复现（回归 c）

def load_reference(path: str) -> dict:
    txt = Path(path).read_text(encoding="utf-8")
    refs = {}
    for m in re.finditer(r"### PDF 第 (\d+) 页\n(.*?)(?=### |\Z)", txt, re.S):
        paras = m.group(2).strip().split("\n\n")
        refs[int(m.group(1))] = "".join(paras[1:])
    return refs


def edit_distance_ops(ref: str, got: str):
    import difflib
    sm = difflib.SequenceMatcher(None, ref, got, autojunk=False)
    ops = [(tag, ref[i1:i2], got[j1:j2], i1)
           for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal"]
    matched = sum(b.size for b in sm.get_matching_blocks())
    return len(ref) - matched, ops


def cer_eval(pdf: str, ref_path: str, pages=(41, 121)) -> int:
    if not Path(pdf).is_file():
        print("SKIP：PDF 不存在 %s" % pdf)
        return 2
    if not Path(ref_path).is_file():
        print("SKIP：参考稿不存在 %s" % ref_path)
        return 2
    refs = load_reference(ref_path)
    fails = []
    with tempfile.TemporaryDirectory() as td:
        with VO.VerticalOCR(pdf, scale=3.0, ears=["某城风物志", "风物志", "某城风物志"]) as ocr:
            ocr.ears = ["某城风物志", "风物志"]
            for pg in pages:
                r = ocr.recognize(pg, use_cache=False)
                ref = refs[pg]
                raw = "".join(c for c in r["text"] if VO.is_cjk(c))
                ref_c = "".join(c for c in ref if VO.is_cjk(c))
                e_raw, _ = edit_distance_ops(ref_c, raw)
                rn, gn = VO.norm_variants(ref_c), VO.norm_variants(raw)
                e_norm, ops = edit_distance_ops(rn, gn)
                ref_p = {c: ref.count(c) for c in "、。，"}
                got_p = {c: r["text"].count(c) for c in "、。，"}
                dev = sum(abs(got_p[c] - ref_p[c]) for c in ref_p) / max(1, sum(ref_p.values()))
                bad = sum(r["text"].count(c) for c in "’°_O")
                base = S1_BASE[pg]
                # 与 S1 基线同为 4 位小数口径（基线值是四舍五入后的）
                cer_raw = e_raw / max(1, len(ref_c))
                cer_norm = e_norm / max(1, len(rn))
                ok = (round(cer_raw, 4) <= base["cer_raw"]
                      and round(cer_norm, 4) <= base["cer_norm"]
                      and round(dev, 4) <= base["punct_dev"] and bad == 0)
                print("p%-4d 汉字 %d/%d | 原始 CER %5.2f%%（基线 %4.2f%%）| 归一 %5.2f%%（基线 %4.2f%%）"
                      " | 标点偏差 %4.1f%%（基线 %4.1f%%）| 杂符 %d | %s" % (
                          pg, len(raw), len(ref_c), cer_raw * 100,
                          base["cer_raw"] * 100, cer_norm * 100,
                          base["cer_norm"] * 100, dev * 100, base["punct_dev"] * 100, bad,
                          "PASS" if ok else "FAIL"))
                if not ok:
                    fails.append((pg, ops[:8]))
    for pg, ops in fails:
        print("  p%d 归一后差异样例：" % pg)
        for op in ops:
            print("   ", op)
    print("回归 c 结论：", "PASS（两页 CER/标点/杂符均不劣化）" if not fails else "FAIL")
    return 0 if not fails else 1


if __name__ == "__main__":
    if "--cer" in sys.argv:
        pdf = os.environ.get("VERTICAL_EVAL_PDF", DEFAULT_PDF)
        ref = os.environ.get("VERTICAL_EVAL_REF", DEFAULT_REF)
        sys.exit(cer_eval(pdf, ref))
    unittest.main(verbosity=2)