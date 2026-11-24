"""Regression tests for diagnostics and review, using disposable files."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

import clean_watermark_images
import figure_audit
import figgap
import fix_captions
import fix_readability
import pipeline
import realign_chapters
import review_batch


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class ToolRegressions(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.book = self.root / "book"
        self.book.mkdir()
        self.pages = self.book / "pages"
        self.pages.mkdir()
        self.assets = self.book / "assets"
        self.assets.mkdir()
        self.manuscript = self.book / "book.json"

    def figure(self, number, y, color="blue", size=(60, 60)):
        name = f"fig-{number:03}"
        src = f"assets/{name}.webp"
        Image.new("RGB", size, color).save(self.book / src)
        return {"type": "figure", "page": 1, "id": name, "src": src, "caption": "",
                "box": [0, y, size[0], y + size[1]]}

    def test_caption_dry_leaves_chained_merge_assets_untouched(self):
        figures = [self.figure(1, 0), self.figure(2, 62, "red"), self.figure(3, 124, "green")]
        write_json(self.manuscript, {"stats": {}, "blocks": figures})
        original = self.manuscript.read_bytes()
        assets = {file.name: file.read_bytes() for file in self.assets.iterdir()}
        outcome = fix_captions.run(str(self.book), do_ocr=False, dry=True)
        self.assertEqual(len(outcome["合并计划"]), 2)
        self.assertEqual(self.manuscript.read_bytes(), original)
        self.assertEqual({file.name: file.read_bytes() for file in self.assets.iterdir()}, assets)

    def test_cleanfigs_leaves_original_assets_and_records_reason(self):
        figure = self.figure(1, 0, color="white")
        write_json(self.manuscript, {"stats": {}, "blocks": [figure]})
        before = self.manuscript.read_bytes()
        reasons, _ = clean_watermark_images.clean_figures(str(self.book), dry=True)
        self.assertEqual(reasons[0]["reason"], "light")
        self.assertEqual(reasons[0]["action"], "review")
        self.assertEqual(self.manuscript.read_bytes(), before)
        clean_watermark_images.clean_figures(str(self.book))
        self.assertEqual(self.manuscript.read_bytes(), before)
        clean_watermark_images.clean_figures(str(self.book), confirmed=[figure["id"]])
        self.assertEqual(json.loads(self.manuscript.read_text(encoding="utf-8"))["blocks"], [])
        self.assertTrue((self.book / figure["src"]).is_file())
        receipts = [json.loads(path.read_text(encoding="utf-8"))
                    for path in (self.book / "figure-cleanup").glob("*.json")]
        self.assertIn([figure], [receipt["removed_blocks"] for receipt in receipts])

    def test_caption_ocr_candidate_stays_in_manuscript(self):
        figure = self.figure(1, 0)
        write_json(self.manuscript, {"stats": {}, "blocks": [figure]})
        with patch.object(pipeline, "book_profile", return_value={"classes": ["B"]}), \
             patch.object(fix_captions, "text_lines", return_value=3):
            outcome = fix_captions.run(str(self.book))
        self.assertEqual(outcome["疑似文字误检图块"][0]["id"], figure["id"])
        self.assertEqual(json.loads(self.manuscript.read_text(encoding="utf-8"))["blocks"], [figure])

    def test_readability_has_no_implicit_page_or_figure_override(self):
        figure = self.figure(220, 0, size=(500, 90))
        paragraph = {"page": 414, "type": "paragraph", "text": "保留第414页的正文。"}
        write_json(self.manuscript, {"blocks": [figure, paragraph], "stats": {}, "chapters": [],
                   "pageRange": [1, 414], "bookTitle": "测试书"})
        with patch("sys.argv", ["fix_readability.py", str(self.book)]):
            fix_readability.main()
        book = json.loads(self.manuscript.read_text(encoding="utf-8"))
        self.assertTrue(any(block["page"] == 414 for block in book["blocks"]))
        self.assertEqual(next(block for block in book["blocks"] if block["type"] == "figure")["caption"], "")

    def test_textcheck_reads_source_pages_and_flags_off_page_text(self):
        missing = "青石走廊尽头设有两扇拱窗河岸地基采用竹篾夯土"
        moved = "后庭屋顶采用榫卯构造外墙布置四处木质百叶"
        write_json(self.manuscript, {"stats": {"pages": 4},
                   "blocks": [{"page": 2, "type": "paragraph", "text": moved}]})
        write_json(self.pages / "page_001_ocr2.json", [{"text": moved}])
        write_json(self.pages / "page_002_ocr2.json", [{"text": moved}])
        write_json(self.pages / "page_003_ocr.json", [{"text": missing}])
        (self.pages / "page_003_ocr2.json").write_text("INVALID", encoding="utf-8")
        outcome = pipeline.textcheck(self.book)
        self.assertEqual(outcome["_ocr_lines"], 3)
        self.assertEqual(outcome["no_block_pages"], [1, 3])
        self.assertEqual(outcome["missing_cache_pages"], [4])
        self.assertEqual(outcome["legacy_cache_pages"], [3])
        self.assertEqual(outcome["other_page_lines"], [(1, moved, [2])])
        self.assertEqual(outcome["lost_lines"], [(3, missing)])

    def test_figgap_distinguishes_caption_reference_and_existing_assets(self):
        figure = self.figure(1, 0)
        figure.update({"page": 2, "originalNumber": "5-67", "displayCaption": "图 1"})
        missing = {"type": "figure", "page": 2, "src": "assets/gone.webp", "originalNumber": "5-71"}
        write_json(self.manuscript, {"blocks": [figure, missing], "figureMap": {"5-72": "图 1"}})
        write_json(self.pages / "page_001_ocr2.json", [{"text": text} for text in (
            "图5-67是美国芝加哥的建筑", "图5-67 芝加哥的高层建筑",
            "图5-70 平面图", "图5-71 剖面图", "图5-72 建筑外观")])
        result = figgap.analyze(self.book)
        self.assertEqual(result["matched"], 1)
        self.assertEqual([item["reason"] for item in result["excluded"]], ["prose_reference"])
        self.assertEqual([(item["number"], item["reason"]) for item in result["candidates"]],
                         [("5-70", "missing_figure"), ("5-71", "missing_asset"), ("5-72", "missing_figure")])

    def test_figgap_recovers_prefixed_caption_with_independent_nearby_reference(self):
        write_json(self.manuscript, {"blocks": []})
        lines = [{"text": text, "x0": 10, "y0": 20 + i * 20, "x1": 120, "y1": 35 + i * 20}
                 for i, text in enumerate(("建筑的内部形象，图5-16为穹顶。", "皮图5-16 万神庙内景",
                                           "皮图5-17 无佐证", "见图5-16 细部",
                                           "皮图5-18为建筑入口", "正文中图5-18为建筑入口。"))]
        write_json(self.pages / "page_001_ocr2.json", lines)
        self.assertEqual(figgap.analyze(self.book, recover_prefix=False)["candidates"], [])
        result = figgap.analyze(self.book)["candidates"]
        self.assertEqual([item["number"] for item in result], ["5-16"])
        self.assertEqual(result[0]["supporting_reference"], {"page": 1, "text": lines[0]["text"]})

    def test_figgap_separates_page_pointers_without_matching_a_real_caption(self):
        figure = self.figure(1, 0)
        figure.update(caption="图6，见16页", displayCaption="图1")
        write_json(self.manuscript, {"blocks": [figure], "figureMap": {"6": "图1"},
                                     "figure_caption_exempt": True})
        write_json(self.pages / "page_001_ocr2.json", [{"text": text} for text in (
            "图6，见16页", "图8参见第28页", "图6 波士顿中央公园",
        )])
        result = figgap.analyze(self.book)
        self.assertEqual([(item["number"], item["reason"]) for item in result["page_references"]],
                         [("6", "printed_page_reference"), ("8", "printed_page_reference")])
        self.assertEqual([item["number"] for item in result["candidates"]], ["6"])
        self.assertEqual(result["caption_audit"]["numbered_caption_candidates"], 1)
        self.assertEqual(result["caption_audit"]["manuscript_figures_without_printed_number"], 1)
        self.assertTrue(result["caption_audit"]["bookwide_caption_exemption"])

    def test_visual_audit_distinguishes_merged_panel_and_uncertain_coordinates(self):
        asset = {"id": "original", "number": "5-15", "box": [20, 20, 300, 320], "asset_exists": True}
        regions = [{"box": box} for box in ([35, 40, 140, 95], [155, 190, 280, 260], [500, 500, 660, 660])]
        captions = [{"number": "5-16", "text": "图 5-16 内景", "ocr_box": [166, 265, 240, 285]}]
        figure_audit.compare_page(regions, [asset], captions, set(), (800, 800))
        self.assertEqual([item["status"] for item in regions],
                         ["covered", "covered", "suspected_missing_figure"])

        unknown = dict(asset, id="legacy-no-box", box=None)
        self.assertEqual(figure_audit.compare_page(regions, [asset, unknown], captions, {"5-16"}, (800, 800)),
                         ["legacy-no-box"])
        self.assertEqual([item["status"] for item in regions],
                         ["covered", "suspected_merged_figure", "unverified_geometry"])

    def test_visual_audit_distinguishes_large_model_box_from_partial_numbered_crop(self):
        drawings = [
            {"id": "fig-076", "number": None, "box": [1127, 1367, 1370, 1547], "asset_exists": True},
            {"id": "fig-077", "number": None, "box": [1142, 1622, 1385, 1882], "asset_exists": True},
        ]
        region = {"box": [1136, 1387, 1384, 1895]}
        figure_audit.compare_page([region], drawings, [], set(), (1451, 2094))
        self.assertEqual(region["status"], "covered_by_multiple_assets")
        self.assertGreater(region["multiple_asset_coverage"], 0.79)

        columns = [
            {"id": "fig-199", "number": "5-14", "box": [182, 294, 490, 554], "asset_exists": True},
            {"id": "fig-200", "number": "5-15", "box": [534, 278, 1274, 1882], "asset_exists": True},
        ]
        region = {"box": [192, 291, 1189, 556]}
        caption = {"number": "5-14", "text": "图5-14 古希腊的三种柱式", "ocr_box": [115, 565, 423, 593]}
        figure_audit.compare_page([region], columns, [caption], set(), (1353, 2131))
        self.assertEqual(region["status"], "suspected_partial_crop")

    def test_recovers_missing_asset_box_and_saves_evidence_for_low_confidence(self):
        from PIL import ImageDraw
        from figure_asset_boxes import recover_page_boxes

        image = Image.new("RGB", (500, 610), "white")
        crop = Image.new("RGB", (160, 180), "white")
        draw = ImageDraw.Draw(crop)
        draw.polygon([(10, 140), (58, 36), (152, 113)], fill="#333333")
        draw.rectangle((79, 135, 124, 168), fill="#555555")
        image.paste(crop, (125, 200))
        crop.save(self.assets / "recognizable.png")
        Image.new("RGB", (130, 135), "white").save(self.assets / "blank.png")
        rows = [
            {"box": None, "asset_exists": True, "src": "assets/recognizable.png"},
            {"box": None, "asset_exists": True, "src": "assets/blank.png"},
        ]
        recover_page_boxes(image, rows, self.book, self.root, 1)
        self.assertTrue(rows[0]["geometry_match"]["accepted"])
        self.assertLessEqual(max(abs(a - b) for a, b in zip(rows[0]["box"], (125, 200, 285, 380))), 2)
        self.assertIsNone(rows[1]["box"])
        self.assertTrue((self.root / rows[1]["geometry_match"]["review_image"]).is_file())

    def test_focused_pages_keeps_risks_and_spreads_negative_samples(self):
        pages = [{"page": page, "regions": [], "figures": [], "unknown_geometry": [], "ocr_lines": 3}
                 for page in range(1, 10)]
        pages[0]["ocr_lines"] = 0
        pages[3]["unknown_geometry"] = ["fig-a"]
        pages[7]["regions"] = [{"status": "suspected_partial_crop"}]
        result = figure_audit.focused_pages(pages, [{"page": 3}], 2)
        self.assertEqual(result["sampled_pages"], [5, 7])
        self.assertEqual(result["unselected_pages"], [2, 6, 9])
        selected = {row["page"]: row["reasons"] for row in result["selected"]}
        self.assertEqual(selected[1], ["no_ocr_lines"])
        self.assertEqual(selected[3], ["caption_alert"])
        self.assertEqual(selected[4], ["unverified_geometry"])
        self.assertEqual(selected[8], ["legacy_visual_alert"])

    def test_visual_audit_checks_missing_ocr_pages_with_pdf_page_offset(self):
        from collections import defaultdict

        pdf = self.root / "source.pdf"
        first = Image.new("RGB", (100, 120), "white")
        second = Image.new("RGB", (140, 160), "white")
        first.save(pdf, save_all=True, append_images=[second])
        out = self.root / "evidence"
        out.mkdir()
        detector = (lambda image, lines: [], {"backend": "fixture"})
        report = figure_audit.audit_pdf(
            pdf, self.book, {"pageRange": [2, 3]}, {3: ("ocr2", [{"text": "测试"}])},
            defaultdict(list), [], out, detector, page_offset=-1, ocr_scale=1,
        )
        self.assertEqual([(row["page"], row["pdf_page"], row["width"], row["ocr_lines"]) for row in report["pages"]],
                         [(2, 1, 100, 0), (3, 2, 140, 1)])

    def test_execute_batch_snapshots_and_reanchors_once(self):
        first = {"id": "title", "page": 1, "type": "heading", "text": "章首"}
        second = {"page": 2, "type": "paragraph", "text": "保留的正文"}
        write_json(self.manuscript, {"stats": {}, "blocks": [first, second],
                   "chapters": [{"id": "ch-1", "startBlock": 0, "endBlock": 1, "blocks": [first, second]}]})
        bank = self.root / "bank.json"
        write_json(bank, {"questions": [{"id": "q1", "page": 2, "block": 1, "chapter": "ch-1"}]})
        before, bank_before = self.manuscript.read_bytes(), bank.read_bytes()
        plan = self.root / "ready.json"
        write_json(plan, {"apply": True, "expected_book_sha256": hashlib.sha256(before).hexdigest(),
                   "book_dir": str(self.book), "regions": [{
                       "page": 1, "start_prefix": "章首", "blocks": [
                           first, {"page": 1, "type": "paragraph", "text": "插入的新正文"}
                       ]
                   }]})
        out = self.root / "batch"
        with patch.object(review_batch, "verify_batch") as verify:
            status = review_batch.execute_batch(plan, out, bank)
            verify.assert_called_once()
        self.assertEqual((out / "book-before.json").read_bytes(), before)
        self.assertEqual((out / "bank-before.json").read_bytes(), bank_before)
        self.assertEqual(json.loads(bank.read_text(encoding="utf-8"))["questions"][0]["block"], 2)
        revised = json.loads(self.manuscript.read_text(encoding="utf-8"))
        self.assertEqual(revised["chapters"][0]["endBlock"], 2)
        realign_chapters.check_bounds(revised)
        self.assertEqual(json.loads(status.read_text(encoding="utf-8"))["status"], "checks_run")
        with self.assertRaisesRegex(ValueError, "已变化"):
            review_batch.execute_batch(plan, self.root / "rerun", bank)

    def test_execute_batch_stops_before_publishing_unresolved_anchor(self):
        old = {"page": 1, "type": "paragraph", "text": "原依据"}
        write_json(self.manuscript, {"stats": {}, "blocks": [old], "chapters": []})
        bank = self.root / "bank.json"
        write_json(bank, {"questions": [{"id": "q1", "page": 1, "block": 0}]})
        before, bank_before = self.manuscript.read_bytes(), bank.read_bytes()
        plan = self.root / "ready.json"
        write_json(plan, {"apply": True, "expected_book_sha256": hashlib.sha256(before).hexdigest(),
                   "book_dir": str(self.book), "regions": [{
                       "page": 1, "start_prefix": "原依据", "blocks": [dict(old, text="换过的依据")]
                   }]})
        out = self.root / "unresolved"
        with self.assertRaisesRegex(ValueError, "需核对新锚点"):
            review_batch.execute_batch(plan, out, bank)
        self.assertEqual(self.manuscript.read_bytes(), before)
        self.assertEqual(bank.read_bytes(), bank_before)
        self.assertTrue((out / "anchor-review.json").is_file())


if __name__ == "__main__":
    unittest.main()
