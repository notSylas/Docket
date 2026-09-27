"""Tests for `docket.infra.parsing.formula_crop` -- cropping and size-filtering of
individual detected formula regions (Phase B checkpoint 1 of "verified
formula transcription").

Uses small synthetic PIL images (no Docling/real-PDF dependency needed for
these pure geometry/PIL tests) with known page dimensions and bboxes, so
expected crop boxes can be hand-computed and asserted exactly.
"""

from __future__ import annotations

from io import BytesIO

from PIL import Image

from docket.infra.parsing.docling_wrapper import PAGE_IMAGES_SCALE
from docket.infra.parsing.formula_crop import (
    FORMULA_CROP_PADDING_PX,
    MIN_FORMULA_REGION_AREA_PT2,
    crop_formula_region,
    formula_region_area_pt2,
    is_transcribable,
)


def _page_png(width_px: int, height_px: int, color=(255, 255, 255)) -> bytes:
    image = Image.new("RGB", (width_px, height_px), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _region(*, l, t, r, b, page_width=300.0, page_height=400.0, origin="BOTTOMLEFT", page_no=1):
    return {
        "item_ref": "#/texts/0",
        "page_no": page_no,
        "coordinate_origin": origin,
        "page_width": page_width,
        "page_height": page_height,
        "bbox": {"l": l, "t": t, "r": r, "b": b},
    }


# ---------------------------------------------------------------------------
# formula_region_area_pt2 / is_transcribable
# ---------------------------------------------------------------------------


def test_formula_region_area_pt2_computes_width_times_height():
    region = _region(l=10, t=50, r=20, b=40)  # width=10, height=10
    assert formula_region_area_pt2(region) == 100.0


def test_formula_region_area_pt2_zero_for_missing_bbox():
    region = _region(l=10, t=50, r=20, b=40)
    region["bbox"] = None
    assert formula_region_area_pt2(region) == 0.0


def test_is_transcribable_below_threshold_is_false():
    # A lone single-glyph-sized region, well under MIN_FORMULA_REGION_AREA_PT2
    # (real-world example: the "m" region measured on leph103.pdf was 87.9
    # pt^2 -- see MIN_FORMULA_REGION_AREA_PT2's docstring).
    region = _region(l=0, t=10, r=9, b=0)  # 9 x 10 = 90 pt^2
    assert formula_region_area_pt2(region) < MIN_FORMULA_REGION_AREA_PT2
    assert is_transcribable(region) is False


def test_is_transcribable_above_threshold_is_true():
    # Real-world example: "P = I V" measured at 324.1 pt^2 on leph103.pdf.
    region = _region(l=0, t=10, r=32, b=0)  # 32 x 10 = 320 pt^2
    assert formula_region_area_pt2(region) < 325 and formula_region_area_pt2(region) > MIN_FORMULA_REGION_AREA_PT2
    assert is_transcribable(region) is True


def test_is_transcribable_exactly_at_threshold_is_true():
    region = _region(l=0, t=MIN_FORMULA_REGION_AREA_PT2, r=1, b=0)
    assert formula_region_area_pt2(region) == MIN_FORMULA_REGION_AREA_PT2
    assert is_transcribable(region) is True


# ---------------------------------------------------------------------------
# crop_formula_region -- pixel-coordinate conversion, Y-flip, padding, bounds
# ---------------------------------------------------------------------------


def test_crop_bottomleft_origin_flips_y_and_converts_scale():
    """A region in the middle of the page: hand-computed expected pixel box.

    page: 300x400 pt, rendered at scale=2.0 -> 600x800 px image.
    bbox (BOTTOMLEFT): l=100, t=300, r=150, b=280 (measured from bottom).
    Expected top-left pixel box (no padding):
      left   = 100 * 2 = 200
      top    = (400 - 300) * 2 = 200   (flip: page_height - t)
      right  = 150 * 2 = 300
      bottom = (400 - 280) * 2 = 240   (flip: page_height - b)
    With padding=0 explicitly, the crop size must be exactly (100, 40).
    """
    page_bytes = _page_png(600, 800)
    region = _region(l=100, t=300, r=150, b=280, page_width=300.0, page_height=400.0)

    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=0)
    cropped = Image.open(BytesIO(cropped_bytes))

    assert cropped.size == (100, 40)


def test_crop_padding_expands_box_symmetrically_within_bounds():
    page_bytes = _page_png(600, 800)
    region = _region(l=100, t=300, r=150, b=280, page_width=300.0, page_height=400.0)

    unpadded = Image.open(BytesIO(crop_formula_region(page_bytes, region, padding_px=0)))
    padded = Image.open(BytesIO(crop_formula_region(page_bytes, region, padding_px=5)))

    # 5px padding on every side -> +10 to both width and height, away from
    # any image edge (this region sits comfortably inside a 600x800 image).
    assert padded.size == (unpadded.width + 10, unpadded.height + 10)


def test_crop_uses_default_padding_and_scale_constants():
    """Default-argument call must match FORMULA_CROP_PADDING_PX/PAGE_IMAGES_SCALE
    exactly -- proves the defaults aren't silently drifting from the
    constants a caller would otherwise have to pass explicitly."""
    page_bytes = _page_png(600, 800)
    region = _region(l=100, t=300, r=150, b=280, page_width=300.0, page_height=400.0)

    default_crop = Image.open(BytesIO(crop_formula_region(page_bytes, region)))
    explicit_crop = Image.open(
        BytesIO(
            crop_formula_region(
                page_bytes, region, images_scale=PAGE_IMAGES_SCALE, padding_px=FORMULA_CROP_PADDING_PX
            )
        )
    )
    assert default_crop.size == explicit_crop.size


def test_crop_region_flush_against_top_left_corner_no_crash():
    """A region touching the page's top-left corner (in pixel space): with
    BOTTOMLEFT origin, this means t == page_height (top edge) and l == 0."""
    page_bytes = _page_png(600, 800)
    region = _region(l=0, t=400.0, r=20, b=390, page_width=300.0, page_height=400.0)

    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=4)
    cropped = Image.open(BytesIO(cropped_bytes))

    # Padding must be clamped, not pushed negative/out of range.
    assert cropped.width > 0 and cropped.height > 0
    assert cropped.width <= 40 + 8  # region width*scale + at most 2*padding


def test_crop_region_flush_against_bottom_right_corner_no_crash():
    """A region touching the page's bottom-right corner. With BOTTOMLEFT
    origin, b=0 is the very bottom of the page, r=page_width is the right
    edge."""
    page_bytes = _page_png(600, 800)
    region = _region(l=280, t=10, r=300, b=0, page_width=300.0, page_height=400.0)

    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=4)
    cropped = Image.open(BytesIO(cropped_bytes))

    assert cropped.width > 0 and cropped.height > 0
    # Right/bottom edges must be clamped to the actual 600x800 image, never
    # extending past it despite the padding.
    assert cropped.width <= 40 + 8
    assert cropped.height <= 20 + 8


def test_crop_region_flush_against_right_edge_padding_clamped():
    page_bytes = _page_png(600, 800)
    # r == page_width (right edge); padding must not push the box past 600px.
    region = _region(l=250, t=200, r=300, b=180, page_width=300.0, page_height=400.0)

    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=4)
    cropped = Image.open(BytesIO(cropped_bytes))
    assert cropped.width > 0 and cropped.height > 0


def test_crop_region_flush_against_bottom_page_edge_padding_clamped():
    page_bytes = _page_png(600, 800)
    # b == 0 (very bottom of the page under BOTTOMLEFT); flipped pixel
    # bottom = (page_height - 0) * scale = image height exactly.
    region = _region(l=100, t=20, r=150, b=0, page_width=300.0, page_height=400.0)

    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=4)
    cropped = Image.open(BytesIO(cropped_bytes))
    assert cropped.width > 0 and cropped.height > 0


def test_crop_topleft_origin_no_y_flip():
    """A non-BOTTOMLEFT (e.g. TOPLEFT) origin is treated as already
    top-left pixel space -- no flip applied."""
    page_bytes = _page_png(600, 800)
    region = _region(
        l=100, t=100, r=150, b=140, page_width=300.0, page_height=400.0, origin="TOPLEFT"
    )
    # TOPLEFT: top=100 (smaller), bottom=140 (larger) -- no flip.
    cropped_bytes = crop_formula_region(page_bytes, region, padding_px=0)
    cropped = Image.open(BytesIO(cropped_bytes))
    assert cropped.size == (100, 80)  # (150-100)*2, (140-100)*2


def test_crop_returns_real_png_bytes():
    page_bytes = _page_png(600, 800)
    region = _region(l=100, t=300, r=150, b=280, page_width=300.0, page_height=400.0)
    cropped_bytes = crop_formula_region(page_bytes, region)
    assert cropped_bytes.startswith(b"\x89PNG\r\n\x1a\n")
