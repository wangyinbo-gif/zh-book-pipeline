"""Locate legacy manuscript assets on the same rendered PDF page."""

from hashlib import sha256
from io import BytesIO

from figure_audit import area, bounded_box, intersection

SCALES = (0.60, 2 / 3, 0.75, 0.80, 1.0, 1.25, 1.5)
PREVIEW_RATIO = 0.60
MIN_SCORE = 0.88
MIN_MARGIN = 0.07


def registration_preview(image):
    import cv2
    import numpy as np

    return cv2.resize(np.asarray(image.convert("L")), None, fx=PREVIEW_RATIO, fy=PREVIEW_RATIO,
                      interpolation=cv2.INTER_AREA)


def locate_asset(image, asset):
    import cv2

    height, width = image.shape
    choices = []
    for scale in SCALES:
        w = round(asset.shape[1] * scale * PREVIEW_RATIO)
        h = round(asset.shape[0] * scale * PREVIEW_RATIO)
        if not (10 <= w <= width and 10 <= h <= height):
            continue
        template = cv2.resize(asset, (w, h), interpolation=cv2.INTER_AREA)
        if template.std() < 7:
            continue
        matches = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
        for _ in range(2):
            _, score, _, (x, y) = cv2.minMaxLoc(matches)
            box = [round(v / PREVIEW_RATIO) for v in (x, y, x + w, y + h)]
            choices.append({"box": box, "score": round(score, 4), "scale": scale})
            dy, dx = max(3, h // 4), max(3, w // 4)
            matches[max(0, y - dy):y + dy + 1, max(0, x - dx):x + dx + 1] = -1
    if not choices:
        return {"box": None, "score": None, "margin": None, "accepted": False}
    best = max(choices, key=lambda row: row["score"])
    best_box = best["box"]
    second = max((row["score"] for row in choices
                  if intersection(best_box, row["box"]) < 0.5 * min(area(best_box), area(row["box"]))),
                 default=0)
    margin = round(best["score"] - second, 4)
    return {**best, "margin": margin,
            "accepted": best["score"] >= MIN_SCORE and margin >= MIN_MARGIN}


def draw_registration_review(page_image, asset, box, output):
    from PIL import Image, ImageDraw

    if box:
        x0, y0, x1, y1 = box
        left = page_image.crop((max(0, x0 - 12), max(0, y0 - 12),
                                min(page_image.width, x1 + 12), min(page_image.height, y1 + 12)))
        ImageDraw.Draw(left).rectangle(
            (min(12, x0), min(12, y0), x1 - max(0, x0 - 12), y1 - max(0, y0 - 12)),
            outline="#c22724", width=2,
        )
    else:
        left = page_image.copy()
    right = asset.convert("RGB")
    for panel in (left, right):
        panel.thumbnail((520, 640))
    sheet = Image.new("RGB", (left.width + right.width + 28, max(left.height, right.height) + 34), "white")
    sheet.paste(left, (6, 26))
    sheet.paste(right, (left.width + 20, 26))
    draw = ImageDraw.Draw(sheet)
    draw.text((6, 6), "Source PDF", fill="black")
    draw.text((left.width + 20, 6), "Stored figure", fill="black")
    sheet.save(output, quality=92)


def recover_page_boxes(image, figures, book_dir, out, page):
    from PIL import Image
    import cv2
    import numpy as np

    pending = [row for row in figures if not row["box"] and row["asset_exists"]]
    if not pending:
        return
    preview = registration_preview(image)
    for index, row in enumerate(pending, 1):
        asset_path = (book_dir / row["src"]).resolve()
        if not asset_path.is_relative_to(book_dir):
            continue
        try:
            raw = asset_path.read_bytes()
            with Image.open(BytesIO(raw)) as opened:
                asset = opened.convert("L")
                match = locate_asset(preview, np.asarray(asset))
        except (OSError, ValueError, Image.DecompressionBombError, cv2.error) as exc:
            row["geometry_match"] = {"accepted": False, "error": str(exc)}
            continue
        match["asset_sha256"] = sha256(raw).hexdigest()
        row["geometry_match"] = match
        box = bounded_box(match["box"], image.size)
        if match["accepted"] and box:
            row["box"] = box
            row["geometry_source"] = "same_page_asset_template"
        else:
            match["accepted"] = False
            name = f"geometry-{page:04d}-{index:03d}.jpg"
            draw_registration_review(image, asset, box, out / name)
            match["review_image"] = name
