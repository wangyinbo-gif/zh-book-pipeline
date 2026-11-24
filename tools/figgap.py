"""Report figure gaps using OCR and optional visual evidence from the source PDF."""

import argparse
from collections import defaultdict
from hashlib import file_digest, sha256
import json
from math import isfinite
from pathlib import Path
import re
import unicodedata

from pipeline import load_ocr_pages


NUMBER = r"\d{1,4}(?:\s*[-－—–.·．]\s*\d{1,4}){0,2}"
LABEL = re.compile(rf"^(?:附图|图版|照片|插图|图)\s*({NUMBER})(?!\d)\s*[:：、.]?\s*(.*)")
REFERENCE = re.compile(r"^(?:所示|显示|说明了|的是|是|为|中[的可]|可见|参见|比较)")
INDEX = re.compile(r"^(?:(?:插图|图片|图版|图)?索引|插图目录)\s*[\d丨|]*$")
PREFIXED_LABEL = re.compile(r"^[\u4e00-\u9fff]\s*(图\s*\d.*)$")
BODY_NUMBER = re.compile(rf"图\s*({NUMBER})(?!\d)")
REFERENCE_INTRO = re.compile(r"(?:参见|可见|如|见|比较|对照|详见)\s*$")
PAGE_POINTER = re.compile(r"^[\s,、:：.]*?(?:详见|参见|见)\s*(?:第\s*)?\d+\s*页[。.!！]?$")


def normalized(value):
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    return re.sub(r"[\s]", "", re.sub(r"[－—–.·．]", "-", text))


def label(text):
    match = LABEL.match(unicodedata.normalize("NFKC", str(text or "")).strip())
    if match:
        return normalized(match[1]), match[2].strip()
    return None


def is_page_pointer(title):
    return bool(PAGE_POINTER.fullmatch(title))


def original_number(block, *, separate_references=True):
    original = block.get("originalNumber")
    if original:
        parsed = label(str(original))
        return parsed[0] if parsed else normalized(original)
    for key in ("originalCaption", "caption"):
        parsed = label(block.get(key))
        if parsed and not REFERENCE.match(parsed[1]) and not (separate_references and is_page_pointer(parsed[1])):
            return parsed[0]
    return None


def source_boxes(lines):
    # Source OCR coordinates are available only when all four box values exist.
    for line in lines:
        coords = [line.get(key) for key in ("x0", "y0", "x1", "y1")]
        if not all(isinstance(value, (int, float)) and isfinite(value) for value in coords):
            continue
        if 0 <= coords[0] < coords[2] and 0 <= coords[1] < coords[3]:
            yield line, coords


def prose_numbers(text):
    text = unicodedata.normalize("NFKC", text)
    for match in BODY_NUMBER.finditer(text):
        if REFERENCE_INTRO.search(text[:match.start()]) or REFERENCE.match(text[match.end():].lstrip()):
            yield normalized(match[1])


def prefixed_caption(text):
    text = unicodedata.normalize("NFKC", text)
    if not text or text[0] in "见如按看据附该下上左参考从和与对":
        return None
    match = PREFIXED_LABEL.match(text)
    parsed = label(match[1]) if match else None
    if parsed:
        number, title = parsed
        if "-" in number and 1 <= len(title) <= 36 and not REFERENCE.match(title):
            if not re.search(r"[。！？；]", title):
                return parsed
    return None


def analyze(book_dir, nearby=1, *, recover_prefix=True):
    book_dir = Path(book_dir).resolve()
    book = json.loads((book_dir / "book.json").read_text(encoding="utf-8-sig"))
    return analyze_snapshot(book_dir, book, load_ocr_pages(book_dir), nearby, recover_prefix)


def analyze_snapshot(book_dir, book, ocr, nearby, recover_prefix, selected_pages=None, captions=None,
                     *, separate_references=True):
    figures = defaultdict(list)
    display_numbers = defaultdict(list)
    numbered_assets = 0
    without_printed_number = 0
    for block in book.get("blocks", []):
        if block.get("type") != "figure":
            continue
        src = block.get("src")
        asset = (book_dir / src).resolve() if src else None
        row = {"page": block.get("page"), "id": block.get("id"), "src": src,
               "asset_exists": bool(asset and asset.is_relative_to(book_dir) and asset.is_file()),
               "original": original_number(block, separate_references=separate_references)}
        if row["original"]:
            figures[row["original"]].append(row)
            numbered_assets += 1
        else:
            without_printed_number += 1
            if separate_references and any(
                parsed and is_page_pointer(parsed[1]) for parsed in
                (label(block.get("originalCaption")), label(block.get("caption")))
            ):
                # A display figure number generated from a margin reference is
                # not evidence that the page contains that printed figure.
                continue
        display = label(block.get("displayCaption"))
        if display:
            display_numbers[display[0]].append(row)
        elif block.get("number"):
            display_numbers[normalized(block["number"])].append(row)
    for printed, display in (book.get("figureMap") or {}).items():
        from_map = label(str(printed))
        printed = from_map[0] if from_map else normalized(printed)
        to_display = label(str(display))
        for row in display_numbers.get(to_display[0] if to_display else normalized(display), []):
            if row["original"] is None and row not in figures[printed]:
                figures[printed].append(row)

    local_assets = defaultdict(set)
    for number, rows in figures.items():
        for row in rows:
            if row["asset_exists"] and type(row["page"]) is int:
                for page in range(row["page"] - nearby, row["page"] + nearby + 1):
                    local_assets[page].add(number)
    index_pages = {
        page for page, (_, lines) in ocr.items()
        if any(INDEX.fullmatch(str(line.get("text") or "").strip()) for line in lines[:8])
    }
    # Repeated book headings can replace the index heading on facing pages.
    index_seeds = sorted(index_pages)
    for left, right in zip(index_seeds, index_seeds[1:]):
        if right - left not in (2, 3):
            continue
        for page in range(left + 1, right):
            titles = [label(line.get("text")) for line in ocr.get(page, ("", []))[1]]
            numbers = {pair[0] for pair in titles if pair}
            if len(numbers) >= 5 and not numbers.intersection(local_assets[page]):
                index_pages.add(page)

    references = defaultdict(list)
    if recover_prefix:
        for page, (_, lines) in ocr.items():
            if page not in index_pages:
                for line in lines:
                    text = str(line.get("text") or "").strip()
                    for number in prose_numbers(text):
                        references[number].append((page, line, text))

    candidates, excluded, page_references = [], [], []
    numbered_captions, number_only = set(), set()
    matched = 0
    for page, (variant, lines) in ocr.items():
        if selected_pages is not None and page not in selected_pages:
            continue
        boxes = {id(line): coords for line, coords in source_boxes(lines)}
        index_page = page in index_pages
        exact = {parsed[0] for line in lines if (parsed := label(line.get("text")))
                 and not REFERENCE.match(parsed[1])
                 and not (separate_references and is_page_pointer(parsed[1]))}
        recovered = set()
        for line in lines:
            text = str(line.get("text") or "").strip()
            parsed = label(text)
            reference = None
            if not parsed and recover_prefix and not index_page and id(line) in boxes:
                weak = prefixed_caption(text)
                if weak and weak[0] not in exact and weak[0] not in recovered:
                    support = [row for row in references.get(weak[0], [])
                               if row[1] is not line and abs(row[0] - page) <= nearby]
                    if support:
                        support_page, _, support_text = min(support, key=lambda row: abs(row[0] - page))
                        reference = {"page": support_page, "text": support_text}
                        parsed = weak
                        recovered.add(weak[0])
            if not parsed:
                continue
            number, title = parsed
            entry = {"page": page, "number": number, "text": text, "ocr": variant,
                     "ocr_box": boxes.get(id(line))}
            if index_page:
                entry["reason"] = "index_entry"
                excluded.append(entry)
                continue
            if separate_references and is_page_pointer(title):
                entry["reason"] = "printed_page_reference"
                page_references.append(entry)
                continue
            if REFERENCE.match(title):
                entry["reason"] = "prose_reference"
                excluded.append(entry)
                continue
            (numbered_captions if title else number_only).add((page, number))
            known = figures.get(number, [])
            local = [row for row in known if type(row["page"]) is int and abs(row["page"] - page) <= nearby]
            if reference:
                entry["supporting_reference"] = reference
            if captions is not None and entry["ocr_box"]:
                captions[page].append(entry)
            if any(row["asset_exists"] for row in local):
                matched += 1
                continue
            entry["reason"] = "missing_asset" if local else "same_number_elsewhere" if known else "missing_figure"
            entry["figure_matches"] = known
            candidates.append(entry)
    return {
        "book": book.get("bookTitle") or book_dir.name, "cached_pages": len(ocr), "matched": matched,
        "excluded": excluded, "page_references": page_references, "candidates": candidates,
        "caption_audit": {
            "numbered_caption_candidates": len(numbered_captions),
            "bare_number_candidates": len(number_only - numbered_captions),
            "manuscript_figures_with_printed_number": numbered_assets,
            "manuscript_figures_without_printed_number": without_printed_number,
            "bookwide_caption_exemption": bool(book.get("figure_caption_exempt")),
            "note": "图号和无图号分开报告。无印刷图号的旧图仍需按原页判断图注是否适用。",
        },
        "note": "仅诊断。无图题、OCR 同源漏识和复杂图版需核原页。",
    }


def page_selection(value):
    pages = set()
    for part in value.split(","):
        found = re.fullmatch(r"(\d+)(?:-(\d+))?", part.strip())
        if not found:
            raise argparse.ArgumentTypeError("页码格式示例：37,139-141")
        start, end = int(found[1]), int(found[2] or found[1])
        if not 1 <= start <= end <= 100000:
            raise argparse.ArgumentTypeError("页码必须为递增的正整数，最大 100000")
        pages.update(range(start, end + 1))
    return pages


def file_sha256(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def source_scale(book_dir, first_page_size, override=None):
    if override is not None:
        return override
    marker = Path(book_dir) / ".pipeline.json"
    try:
        scale = json.loads(marker.read_text(encoding="utf-8-sig")).get("scale")
    except (OSError, ValueError, AttributeError):
        return None
    length = max(first_page_size)
    if type(scale) not in (int, float) or not isfinite(scale) or scale <= 0:
        return None
    return max(1.0, round(4200 / length, 2)) if length * scale > 4200 else scale


def render_evidence(report, pdf_file, destination, book_dir, ocr_scale=None, page_offset=0):
    import pymupdf

    with pymupdf.open(pdf_file) as pdf:
        if len(pdf):
            first = pdf[0].rect
            ocr_scale = source_scale(book_dir, (first.width, first.height), ocr_scale)
        pages = {}
        for item in report["candidates"]:
            page = item["page"]
            pdf_page = page + page_offset
            item["pdf_page"] = pdf_page
            if not 1 <= pdf_page <= len(pdf):
                item["evidence_error"] = "页码超出提供的 PDF"
                continue
            if page not in pages:
                pix = pdf[pdf_page - 1].get_pixmap(matrix=pymupdf.Matrix(3, 3), alpha=False)
                name = f"page-{page:04d}.png"
                pix.save(destination / name)
                pages[page] = (name, pix.width, pix.height)
            name, width, height = pages[page]
            item["source_page"] = name
            box = item["ocr_box"]
            if box and ocr_scale:
                bottom = round(box[1] * 3 / ocr_scale)
                if 0 < bottom < height:
                    item["crop_candidate"] = [0, max(0, bottom - round(height * 0.4)), width, bottom]
                    item["crop_basis"] = "图题上方的原页区域，供人工定位和重裁"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("book_dir")
    parser.add_argument("--pdf", type=Path, help="对应原 PDF，提供时生成 3× 原页证据")
    parser.add_argument("--out", type=Path, help="新建报告和原页输出目录")
    parser.add_argument("--ocr-scale", type=float, help="原 OCR 渲染倍率，覆盖流水线记录")
    parser.add_argument("--nearby", type=int, default=1, help="核对相邻物理页半径，默认 1")
    parser.add_argument("--book-json", type=Path, help="历史书稿 JSON，OCR 与资源仍取自书稿目录")
    parser.add_argument("--pages", type=page_selection, help="限定审计的书稿页，如 37,139-141")
    parser.add_argument("--pdf-page-offset", type=int, default=0, help="PDF 物理页减书稿页，默认 0")
    parser.add_argument("--exact-labels", action="store_true", help="仅认行首图题，用于回放原规则")
    parser.add_argument("--visual", choices=("legacy", "paddle", "yolo"), help="用原页区域检测补充文字诊断")
    parser.add_argument("--recover-boxes", action="store_true", help="只读回配无裁框的旧图资产")
    parser.add_argument("--focused", action="store_true", help="整书快筛，再用模型补查重点页与抽样页，含旧图回配")
    parser.add_argument("--focus-sample", type=int, help="重点模式从无预警页均匀抽查的数量，默认 12")
    parser.add_argument("--model-dir", type=Path, help="本地 PP-DocLayoutV3 推理模型目录")
    parser.add_argument("--model-file", type=Path, help="本地 DocLayout-YOLO .pt 权重")
    args = parser.parse_args()
    if args.nearby < 0 or (args.pdf and not args.out):
        parser.error("--nearby 必须非负，--pdf 需要 --out")
    if args.ocr_scale is not None and (not isfinite(args.ocr_scale) or args.ocr_scale <= 0):
        parser.error("--ocr-scale 必须是正数")
    if args.visual and not args.pdf:
        parser.error("--visual 需要 --pdf 和 --out")
    if args.recover_boxes and not args.visual:
        parser.error("--recover-boxes 需要 --visual")
    if args.focused and args.visual not in ("paddle", "yolo"):
        parser.error("--focused 需要 --visual paddle 或 yolo")
    if args.focus_sample is not None and (not args.focused or args.focus_sample < 0):
        parser.error("--focus-sample 需要 --focused，且数量必须非负")
    if args.pdf_page_offset and not args.pdf:
        parser.error("--pdf-page-offset 需要 --pdf")
    if bool(args.model_dir) != (args.visual == "paddle"):
        parser.error("--visual paddle 必须与 --model-dir 一起使用")
    if bool(args.model_file) != (args.visual == "yolo"):
        parser.error("--visual yolo 必须与 --model-file 一起使用")
    book_dir = Path(args.book_dir).resolve()
    book_path = (args.book_json or book_dir / "book.json").resolve()
    raw = book_path.read_bytes()
    book = json.loads(raw.decode("utf-8-sig"))
    ocr = load_ocr_pages(book_dir)
    captions = defaultdict(list) if args.visual else None
    report = analyze_snapshot(book_dir, book, ocr, args.nearby, not args.exact_labels, args.pages, captions,
                              separate_references=not args.exact_labels)
    report["source"] = {"book_json": str(book_path), "book_sha256": sha256(raw).hexdigest(),
                        "text_rule": "exact" if args.exact_labels else "with_supported_prefix",
                        "page_reference_rule": "legacy" if args.exact_labels else "separate"}
    if args.pages:
        report["source"]["pages"] = sorted(args.pages)
    if args.out:
        out = args.out.resolve()
        if out.is_relative_to(book_dir):
            parser.error("--out 请使用书稿目录之外的位置")
        if args.pdf:
            args.pdf = args.pdf.resolve()
            report["source"].update({"pdf": str(args.pdf), "pdf_sha256": file_sha256(args.pdf),
                                     "pdf_page_offset": args.pdf_page_offset})
        out.mkdir(parents=True, exist_ok=False)
        if args.visual:
            from figure_audit import audit_pdf, focused_pages, load_detector

            try:
                inputs = (args.pdf, book_dir, book, ocr, captions, report["candidates"], out)
                options = {"selected_pages": args.pages, "ocr_scale": args.ocr_scale,
                           "page_offset": args.pdf_page_offset, "recover_boxes": args.recover_boxes or args.focused}
                if args.focused:
                    screen = audit_pdf(*inputs, load_detector("legacy"), **options, overlay_prefix="prescreen")
                    report["visual_prescreen"] = screen
                    sample_size = 12 if args.focus_sample is None else args.focus_sample
                    focus = focused_pages(screen["pages"], report["candidates"], sample_size)
                    report["focus"] = focus
                    options["previous_pages"] = {row["page"]: row for row in screen["pages"]}
                    options["selected_pages"] = {row["page"] for row in focus["selected"]}
                if args.focused and not options["selected_pages"]:
                    report["visual"] = {"backend": args.visual, "pages": [], "candidates": [], "seconds": 0,
                                        "skipped_reason": "没有需补查或抽样的页"}
                else:
                    detector = load_detector(args.visual, args.model_dir or args.model_file)
                    report["visual"] = audit_pdf(*inputs, detector, **options)
            except (ImportError, OSError, ValueError, RuntimeError) as exc:
                parser.exit(1, f"原页审查失败：{exc}\n")
        if args.pdf:
            render_evidence(report, args.pdf, out, book_dir, args.ocr_scale, args.pdf_page_offset)
        (out / "figgap-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"文字候选 {len(report['candidates'])} 项")
        if args.focused:
            print(f"快筛 {len(screen['pages'])} 页 · 模型补查 {len(report['visual']['pages'])} 页")
        if args.visual:
            print(f"视觉候选 {len(report['visual']['candidates'])} 项")
        print(f"报告 {out / 'figgap-report.json'}")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
