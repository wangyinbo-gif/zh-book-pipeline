"""A1 新增 gate 判据的回归测试（章结构下限 / 可读性抽样 / 句读密度 / 整页图版豁免）。

阈值与标定依据见 `交接文档\\流程\\gate-判据增补_v1_20261125.md`。
"""
import json
import tempfile
import unittest
from pathlib import Path

import pipeline


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def book(blocks, chapters=1, pages=100):
    figs = sum(1 for b in blocks if b.get("type") == "figure")
    return {
        "stats": {"pages": pages, "chapters": chapters, "figures": figs, "paired": 0},
        "chapters": [{"title": f"第{i + 1}章", "chars": 1000} for i in range(chapters)],
        "blocks": blocks,
    }


def paras(text, n, pages=100):
    """把 text 切成 n 段，页号 1..pages 轮转。"""
    step = max(1, len(text) // n)
    return [{"type": "paragraph", "page": (i % pages) + 1, "text": text[i * step:(i + 1) * step]}
            for i in range(n)]


LONG = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏闰余成岁律吕调阳。" * 4000


def checks_of(root, blocks, **kw):
    write_json(Path(root) / "book.json", book(blocks, **kw))
    _ok, checks = pipeline.gate(str(root), write_profile=False)
    return {c["name"]: c for c in checks}


class GateRules(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = folder.name

    def test_chapter_floor_flags_one_chapter_big_book(self):
        got = checks_of(self.root, paras(LONG, 200), chapters=1)
        self.assertFalse(got["章结构下限"]["ok"], got["章结构下限"]["detail"])

    def test_chapter_floor_allows_small_one_chapter_book(self):
        got = checks_of(self.root, paras(LONG[:30000], 60), chapters=1)
        self.assertTrue(got["章结构下限"]["ok"], got["章结构下限"]["detail"])

    def test_chapter_floor_allows_multi_chapter_big_book(self):
        got = checks_of(self.root, paras(LONG, 200), chapters=3)
        self.assertTrue(got["章结构下限"]["ok"], got["章结构下限"]["detail"])

    def test_readability_flags_scrambled_layout(self):
        # 三个信号同时异常：短块多、平均块长 65 上下、句读密度背离
        blocks = [{"type": "paragraph", "page": i + 1, "text": "名更據了春用"}
                  for i in range(180)]
        blocks += [{"type": "paragraph", "page": 1, "text": "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往"}
                   for _ in range(20)]
        got = checks_of(self.root, blocks, chapters=3)
        self.assertFalse(got["可读性抽样"]["ok"], got["可读性抽样"]["detail"])

    def test_readability_passes_normal_manuscript(self):
        got = checks_of(self.root, paras(LONG, 200), chapters=3)
        self.assertTrue(got["可读性抽样"]["ok"], got["可读性抽样"]["detail"])

    def test_punct_density_flags_missing_punctuation(self):
        text = "天地玄黄宇宙洪荒" * 3000  # 无任何句读
        got = checks_of(self.root, paras(text, 100), chapters=3)
        self.assertFalse(got["句读密度"]["ok"], got["句读密度"]["detail"])

    def test_full_page_plate_auto_exempts_captions(self):
        blocks = []
        for i in range(5):
            blocks.append({"type": "figure", "page": i + 1, "id": f"fig-{i + 1:03}",
                           "src": "assets/x.webp", "caption": "", "number": i + 1,
                           "box": [0, 0, 600, 900], "w": 600, "h": 900})
        blocks += [{"type": "paragraph", "page": 20, "text": "正文一段话。" * 50}]
        got = checks_of(self.root, blocks, chapters=2, pages=20)
        self.assertTrue(got["图注配对"]["ok"], got["图注配对"]["detail"])
        self.assertTrue(got["缺图注"]["ok"], got["缺图注"]["detail"])
        self.assertIn("不适用", got["图注配对"]["detail"])

    def test_regular_figures_still_require_captions(self):
        blocks = []
        for i in range(6):
            blocks.append({"type": "figure", "page": i + 1, "id": f"fig-{i + 1:03}",
                           "src": "assets/x.webp", "caption": "", "number": i + 1,
                           "box": [0, 0, 300, 200], "w": 300, "h": 200})
            blocks.append({"type": "paragraph", "page": i + 1, "text": "图" + str(i + 1) + "　说明文字。" * 8})
        got = checks_of(self.root, blocks, chapters=2, pages=6)
        self.assertFalse(got["图注配对"]["ok"], got["图注配对"]["detail"])

    def test_body_metrics_shape(self):
        b = book(paras(LONG, 50), chapters=2)
        m = pipeline.body_metrics(b)
        for key in ("han", "punct_per100", "avg_blk", "short_rate", "figs", "lonely_share", "body_tu"):
            self.assertIn(key, m)
        self.assertGreater(m["han"], 1000)
        self.assertGreater(m["punct_per100"], 3.0)


if __name__ == "__main__":
    unittest.main()