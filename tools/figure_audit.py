"""Compare figures detected on raw PDF pages against manuscript crops."""

from collections import defaultdict
from contextlib import closing
from itertools import combinations
from math import isfinite
from pathlib import Path
from statistics import median
from time import perf_counter

from figgap import file_sha256, original_number, source_boxes, source_scale

COVERED_FRACTION = 0.75
UNCOVERED_FRACTION = 0.15
SEPARATE_CROP_FRACTION = 0.80


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def intersection(first, second):
    width = max(0, min(first[2], second[2]) - max(first[0], second[0]))
    height = max(0, min(first[3], second[3]) - max(first[1], second[1]))
    return width * height


def contains_multiple_crops(box, pieces):
    for (first, first_area), (second, second_area) in combinations(pieces, 2):
        # Overlapping copies add little evidence. A gap between distinct crops
        # is common when an unnumbered model box encloses two drawings.
        if intersection(first, second) > 0.1 * min(first_area, second_area):
            continue
        common = [
            max(box[0], first[0], second[0]), max(box[1], first[1], second[1]),
            min(box[2], first[2], second[2]), min(box[3], first[3], second[3]),
        ]
        shared = area(common) if common[0] < common[2] and common[1] < common[3] else 0
        cover = (intersection(box, first) + intersection(box, second) - shared) / area(box)
        if cover >= COVERED_FRACTION:
            return round(cover, 4)
    return None


def bounded_box(box, size):
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    if not all(type(value) in (int, float) and isfinite(value) for value in box):
        return None
    width, height = size
    x0, y0, x1, y1 = box
    if x1 <= x0 or y1 <= y0 or x0 < -2 or y0 < -2 or x1 > width + 2 or y1 > height + 2:
        return None
    valid = [max(0, x0), max(0, y0), min(width, x1), min(height, y1)]
    return valid if valid[0] < valid[2] and valid[1] < valid[3] else None


def existing_figures(blocks, book_dir, size):
    rows = []
    for block in blocks:
        src = block.get("src")
        asset = (book_dir / src).resolve() if src else None
        box = bounded_box(block.get("box"), size)
        rows.append({
            "id": block.get("id"),
            "src": src,
            "number": original_number(block),
            "box": box,
            "geometry_source": "manuscript" if box else None,
            "asset_exists": bool(asset and asset.is_relative_to(book_dir) and asset.is_file()),
        })
    return rows


def caption_score(region, caption, size):
    """Lower cost for close captions in the same column or aligned beside a figure."""
    width, height = size
    x0, y0, x1, y1 = region
    cx0, cy0, cx1, cy1 = caption
    x_overlap = max(0, min(x1, cx1) - max(x0, cx0)) / min(x1 - x0, cx1 - cx0)
    y_overlap = max(0, min(y1, cy1) - max(y0, cy0)) / min(y1 - y0, cy1 - cy0)
    gap = max(y0 - cy1, cy0 - y1, 0) / height
    if x_overlap >= 0.35 and gap <= 0.055:
        center_y = (cy0 + cy1) / 2
        edge_distance = min(abs(center_y - y0), abs(center_y - y1)) / height
        if edge_distance <= 0.065:
            return edge_distance + 0.02 * (1 - x_overlap)
    horizontal_gap = max(x0 - cx1, cx0 - x1) / width
    if 0 <= horizontal_gap <= 0.04 and y_overlap >= 0.5:
        return horizontal_gap + 0.02 * (1 - y_overlap)
    return None


def associate_captions(regions, captions, size):
    for caption in captions:
        box = bounded_box(caption["ocr_box"], size)
        if not box:
            continue
        ranked = sorted(
            (score, index) for index, region in enumerate(regions)
            if (score := caption_score(region["box"], box, size)) is not None
        )
        if not ranked:
            continue
        score, index = ranked[0]
        # Ambiguous neighbors are evidence for review, but can't establish a mismatch.
        related = [regions[other]["id"] for cost, other in ranked[1:] if cost - score < 0.006]
        regions[index]["captions"].append({"number": caption["number"], "text": caption["text"],
                                          "box": box, "also_near": related})


def compare_page(regions, figures, captions, unmatched_numbers, size):
    usable = [row for row in figures if row["box"] and row["asset_exists"]]
    unknown = [row["id"] for row in figures if not row["box"] and row["asset_exists"]]
    for index, region in enumerate(regions, 1):
        region.pop("priority", None)
        region.pop("multiple_asset_coverage", None)
        region.update(id=f"R{index}", captions=[])
    associate_captions(regions, captions, size)
    possible_caption_regions = {
        region_id for region in regions for cap in region["captions"]
        for region_id in [region["id"], *cap["also_near"]]
    }

    for region in regions:
        box = region["box"]
        overlaps = []
        separate_crops = []
        for figure in usable:
            old_box = figure["box"]
            old_area = area(old_box)
            shared = intersection(box, old_box)
            if shared:
                overlaps.append({
                    "figure_id": figure["id"],
                    "number": figure["number"],
                    "source_coverage": round(shared / area(box), 4),
                    "iou": round(shared / (area(box) + old_area - shared), 4),
                })
                if shared >= SEPARATE_CROP_FRACTION * old_area and shared >= UNCOVERED_FRACTION * area(box):
                    separate_crops.append((old_box, old_area))
        overlaps.sort(key=lambda match: (match["source_coverage"], match["iou"]), reverse=True)
        region["matches"] = overlaps
        best = overlaps[0] if overlaps else None
        coverage = best["source_coverage"] if best else 0
        printed = {cap["number"] for cap in region["captions"] if not cap["also_near"]}
        different = printed & unmatched_numbers
        if coverage >= COVERED_FRACTION:
            reason = "covered"
            if len(printed) >= 2 or (different and best["number"] and best["number"] not in different):
                old = next(row for row in usable if row["id"] == best["figure_id"])
                reason = ("suspected_merged_figure" if len(printed) >= 2 or area(old["box"]) > 1.5 * area(box)
                          else "suspected_caption_mismatch")
        elif unknown:
            reason = "unverified_geometry"
        elif coverage < UNCOVERED_FRACTION:
            reason = "suspected_missing_figure"
        elif (region["id"] not in possible_caption_regions
              and (combined := contains_multiple_crops(box, separate_crops)) is not None):
            reason = "covered_by_multiple_assets"
            region["multiple_asset_coverage"] = combined
        else:
            reason = "suspected_partial_crop"
        region["status"] = reason
        if reason.startswith("suspected_"):
            region["priority"] = "high" if different else "review"
    return unknown


def focused_pages(pages, text_candidates, sample_size):
    text_pages = {entry["page"] for entry in text_candidates}
    selected = {}
    remaining = []
    for page in pages:
        number = page["page"]
        reasons = []
        if number in text_pages:
            reasons.append("caption_alert")
        if any(region["status"].startswith("suspected_") for region in page["regions"]):
            reasons.append("legacy_visual_alert")
        if page["unknown_geometry"]:
            reasons.append("unverified_geometry")
        if len(page["figures"]) >= 3 or len(page["regions"]) >= 4:
            reasons.append("dense_figures")
        if not page["ocr_lines"]:
            reasons.append("no_ocr_lines")
        if reasons:
            selected[number] = reasons
        else:
            remaining.append(number)
    remaining.sort()
    count = min(sample_size, len(remaining))
    sample = [remaining[(2 * i + 1) * len(remaining) // (2 * count)] for i in range(count)]
    for page in sample:
        selected[page] = ["sample_without_alert"]
    return {"selected": [{"page": page, "reasons": selected[page]} for page in sorted(selected)],
            "sampled_pages": sample, "requested_sample_size": sample_size,
            "unselected_pages": [page for page in remaining if page not in selected]}


def legacy_detector(image, lines):
    from pdf2book import figure_regions

    boxes = [box for _, box in source_boxes(lines) if bounded_box(box, image.size)]
    line_height = median(box[3] - box[1] for box in boxes) if boxes else 40.0
    return [{"box": box, "label": "ink_region"} for box in figure_regions(image, boxes, line_height)]


def _ascii_tempdir():
    """PaddleInference opens the model dir through narrow paths, so the
    staging directory must be ASCII and writable. tempfile.gettempdir() can
    fall back to a Chinese cwd when the user temp dir is not writable."""
    import os
    import tempfile

    candidates = [tempfile.gettempdir()]
    candidates.extend(os.environ.get(name) for name in ("LOCALAPPDATA", "TEMP", "TMP"))
    for candidate in candidates:
        if not candidate or not str(candidate).isascii():
            continue
        try:
            path = Path(candidate)
            probe = path / f".figgap-write-{os.getpid()}"
            probe.write_bytes(b"")
            probe.unlink()
            return str(path)
        except OSError:
            continue
    for letter in "CDEFGH":
        base = Path(letter + ":\\")
        if not base.exists():
            continue
        for sub in ("dsh-ascii-tmp", "figgap-ascii-tmp"):
            path = base / sub
            try:
                path.mkdir(exist_ok=True)
                probe = path / f".figgap-write-{os.getpid()}"
                probe.write_bytes(b"")
                probe.unlink()
                return str(path)
            except OSError:
                continue
    raise RuntimeError("找不到 ASCII 且可写的临时目录（PaddleInference 在 Windows 上要求窄字符模型路径）")


def load_paddle_detector(model_dir):
    from importlib.metadata import version
    import os
    from shutil import copyfile
    from tempfile import TemporaryDirectory

    model_dir = Path(model_dir).resolve()
    model_files = ("inference.json", "inference.pdiparams", "inference.yml")
    for filename in model_files:
        if not (model_dir / filename).is_file():
            raise ValueError(f"模型目录缺少 {filename}：{model_dir}")
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    from paddleocr import LayoutDetection
    import numpy as np

    def create_model(path):
        return LayoutDetection(model_name="PP-DocLayoutV3", model_dir=str(path),
                               device="cpu", enable_mkldnn=False, cpu_threads=4)

    if os.name == "nt" and not str(model_dir).isascii():
        # The Windows static predictor opens model files through narrow paths.
        with TemporaryDirectory(prefix="figgap-model-", dir=_ascii_tempdir()) as temporary:
            for filename in model_files:
                copyfile(model_dir / filename, Path(temporary) / filename)
            model = create_model(temporary)
    else:
        model = create_model(model_dir)
    manifest = {"model": "PP-DocLayoutV3", "device": "cpu", "cpu_threads": 4,
                "paddlepaddle": version("paddlepaddle"), "paddleocr": version("paddleocr"),
                "threshold": 0.5, "classes": ["image", "chart"],
                "model_sha256": {filename: file_sha256(model_dir / filename) for filename in model_files}}

    def detect(image, _lines=None):
        # Paddle's ndarray input uses OpenCV's BGR channel order.
        bgr = np.ascontiguousarray(np.asarray(image.convert("RGB"))[:, :, ::-1])
        result = next(iter(model.predict(bgr, batch_size=1, threshold=manifest["threshold"])))
        return [{"box": [float(n) for n in item["coordinate"]], "label": item["label"],
                 "score": round(float(item["score"]), 4)}
                for item in result["boxes"] if item["label"] in manifest["classes"]]

    return detect, manifest


def load_yolo_detector(model_file):
    from importlib.metadata import version
    import os

    model_file = Path(model_file).resolve()
    if not model_file.is_file() or model_file.suffix.lower() != ".pt":
        raise ValueError("DocLayout-YOLO 需要本地 .pt 模型文件")
    os.environ["YOLO_AUTOINSTALL"] = "false"
    import torch
    from doclayout_yolo import YOLOv10

    torch.set_num_threads(4)
    model = YOLOv10(str(model_file))
    manifest = {"model": "DocLayout-YOLO DocStructBench", "device": "cpu", "cpu_threads": 4,
                "torch": version("torch"), "doclayout_yolo": version("doclayout-yolo"),
                "threshold": 0.5, "image_size": 1024, "classes": ["figure"],
                "model_sha256": file_sha256(model_file)}

    def detect(image, _lines=None):
        result = model.predict(image, imgsz=manifest["image_size"], conf=manifest["threshold"],
                               device="cpu", verbose=False)[0]
        return [{"box": box, "label": result.names[int(class_id)], "score": round(score, 4)}
                for box, class_id, score in zip(result.boxes.xyxy.tolist(), result.boxes.cls.tolist(),
                                                result.boxes.conf.tolist())
                if result.names[int(class_id)] in manifest["classes"]]

    return detect, manifest


def load_detector(backend, model_path=None):
    started = perf_counter()
    if backend == "legacy":
        detect = legacy_detector
        manifest = {"model": "pdf2book.figure_regions", "uses_ocr_mask": True}
    elif backend == "paddle":
        detect, manifest = load_paddle_detector(model_path)
    elif backend == "yolo":
        detect, manifest = load_yolo_detector(model_path)
    else:
        raise ValueError(f"未知的原页检测器：{backend}")
    return detect, dict(manifest, backend=backend, setup_seconds=round(perf_counter() - started, 3))


def draw_overlay(image, figures, regions, destination):
    from PIL import ImageDraw

    draw = ImageDraw.Draw(image)
    for row in figures:
        if row["box"]:
            draw.rectangle(row["box"], outline="#2176ae", width=4)
            draw.text(tuple(row["box"][:2]), str(row["id"]), fill="#2176ae", stroke_width=2, stroke_fill="white")
    for region in regions:
        status = region["status"]
        color = "#c22724" if status.startswith("suspected_") else "#936800" if status == "unverified_geometry" else "#007756"
        draw.rectangle(region["box"], outline=color, width=3)
        x, y = region["box"][:2]
        draw.text((x + 5, y + 13), region["id"], fill=color, stroke_width=2, stroke_fill="white")
        for caption in region["captions"]:
            draw.rectangle(caption["box"], outline="#8051a8", width=3)
    image.save(destination)


def audit_pdf(pdf_path, book_dir, book, ocr, captions, text_candidates, out, detector,
              *, selected_pages=None, ocr_scale=None, page_offset=0, recover_boxes=False,
              previous_pages=None, overlay_prefix="visual"):
    import pypdfium2

    blocks = defaultdict(list)
    known_pages = set(ocr)
    for block in book.get("blocks", []):
        page = block.get("page")
        if type(page) is int and page > 0:
            known_pages.add(page)
            if block.get("type") == "figure":
                blocks[page].append(block)
    unmatched = defaultdict(set)
    for candidate in text_candidates:
        unmatched[candidate["page"]].add(candidate["number"])
    detect, manifest = detector

    pages, candidates = [], []
    started = perf_counter()
    with pypdfium2.PdfDocument(pdf_path) as pdf:
        span = book.get("pageRange")
        if selected_pages is None:
            if (isinstance(span, (list, tuple)) and len(span) == 2
                    and all(type(page) is int for page in span) and 1 <= span[0] <= span[1]):
                selected_pages = range(span[0], span[1] + 1)
            else:
                selected_pages = known_pages or range(max(1, 1 - page_offset), len(pdf) + 1 - page_offset)
        selected = sorted(selected_pages)
        if not selected or selected[0] + page_offset < 1 or selected[-1] + page_offset > len(pdf):
            raise ValueError(f"书稿页加偏移后超出 PDF 页码范围，PDF 共 {len(pdf)} 页。请检查 --pages 和 --pdf-page-offset")
        with closing(pdf[0]) as first_page:
            scale = source_scale(book_dir, first_page.get_size(), ocr_scale)
        if scale is None:
            raise ValueError("需要 .pipeline.json 的渲染倍率，或用 --ocr-scale 指定已知倍率")
        for page in selected:
            pdf_number = page + page_offset
            with closing(pdf[pdf_number - 1]) as pdf_page:
                with closing(pdf_page.render(scale=scale)) as bitmap:
                    image = bitmap.to_pil().convert("RGB")
            prior = previous_pages.get(page) if previous_pages else None
            if prior and (prior["width"], prior["height"], prior["pdf_page"]) == (*image.size, pdf_number):
                figures = prior["figures"]
            else:
                figures = existing_figures(blocks[page], book_dir, image.size)
                if recover_boxes and any(not row["box"] and row["asset_exists"] for row in figures):
                    from figure_asset_boxes import recover_page_boxes

                    recover_page_boxes(image, figures, book_dir, out, page)
            _, lines = ocr.get(page, (None, []))
            regions = []
            for region in detect(image, lines):
                box = bounded_box(region["box"], image.size)
                if box and area(box) > 0:
                    regions.append(dict(region, box=box))
            regions.sort(key=lambda row: (row["box"][1], row["box"][0]))
            unknown = compare_page(regions, figures, captions.get(page, []), unmatched[page], image.size)
            findings = [region for region in regions if region["status"].startswith("suspected_")]
            page_result = {"page": page, "pdf_page": pdf_number, "width": image.width, "height": image.height,
                           "figures": figures, "regions": regions, "unknown_geometry": unknown,
                           "ocr_lines": len(lines)}
            if findings or any(region["status"] in ("unverified_geometry", "covered_by_multiple_assets")
                               for region in regions):
                name = f"{overlay_prefix}-{page:04d}.png"
                draw_overlay(image, figures, regions, out / name)
                page_result["overlay"] = name
                candidates.extend(dict(region, page=page, pdf_page=pdf_number, overlay=name) for region in findings)
            pages.append(page_result)
    return {"backend": manifest["backend"], "detector": manifest, "coordinate_space": "pdf_render_pixels",
            "ocr_scale": scale,
            "recover_boxes": recover_boxes,
            "thresholds": {"covered_source_fraction": COVERED_FRACTION,
                           "uncovered_source_fraction": UNCOVERED_FRACTION},
            "seconds": round(perf_counter() - started, 3), "pages": pages, "candidates": candidates,
            "overlay_colors": {"blue": "书稿裁框", "green": "已覆盖", "red": "疑点",
                               "yellow": "坐标待核", "purple": "关联图题"}}
