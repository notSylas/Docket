"""Cropping and size-filtering for individual detected formula regions.

Phase B checkpoint 1 of "verified formula transcription": this module only
turns a `docling_wrapper._formula_regions()` dict plus its page's already-
rendered PNG bytes (`docling_wrapper._page_images()`) into a small cropped
PNG suitable for a VLM transcription call, and decides which regions are
even worth that call. It does not call any VLM itself (see
`docket.ingestion.formula_transcriber.FormulaTranscriber.transcribe`), and nothing here
ever touches `Chunk.text`, the FTS/vector indexes, `EvidenceResolver`, or
citation validation -- transcriptions produced from these crops are stored
unverified (`EvidenceVersion.formula_transcriptions_json`) and are never
promoted into searchable/citable evidence (see
`Docs/accuracy-evaluation.md`'s "Formula evidence and experiments" section).
"""

from __future__ import annotations

from io import BytesIO

from docket.infra.parsing.docling_wrapper import PAGE_IMAGES_SCALE

# Minimum bbox area, in PDF points^2 (i.e. *before* PAGE_IMAGES_SCALE is
# applied -- these are the same units as `region["bbox"]`/`page_width`/
# `page_height`), below which a detected formula region is judged too
# trivial to be worth a dedicated VLM transcription call.
#
# Empirically measured, not guessed -- against a real ingest of a real
# 26-page NCERT physics chapter (`Reference_Books/leph1dd.zip`'s
# `leph103.pdf`), which produces 93 real formula regions via
# `docling_wrapper._formula_regions`. Sorting all 93 by bbox area in these
# same units and visually inspecting the actual cropped images (not just
# trusting the numbers) showed a sharp, real cliff:
#
#   - Exactly one region (`#/texts/122`, page 7) is genuinely trivial: bbox
#     area 87.9 pt^2, and the crop shows nothing but a lone lowercase "m"
#     (Docling had split one symbol out of some larger expression as its
#     own "formula" text item) -- nothing there for a VLM to transcribe.
#   - Every other region, starting at 324.1 pt^2 ("P = I V", the smallest
#     real multi-symbol equation in this document) and continuing up
#     through several dozen more real equations (checked at small, median,
#     and large bbox sizes across the full 87.9-24821.3 pt^2 range) is a
#     genuine, legible equation worth transcribing.
#
# 150.0 sits in the middle of that real ~3.7x gap (87.9 to 324.1): well
# above the one confirmed-trivial region, well below the smallest
# confirmed-real equation, so this is not a fragile, exact-fit cutoff tuned
# to one number.
MIN_FORMULA_REGION_AREA_PT2 = 150.0

# Padding, in pixels at PAGE_IMAGES_SCALE, added on every side of a region's
# bbox before cropping.
#
# Verified visually, not assumed: cropping the same real region
# (`#/texts/512`, page 17, "V = ε + I r") at padding 0, 4, and 10px showed
# that at 0 the outermost strokes (the top of the "V", the serif of the
# final "I") sit flush against the crop boundary with no breathing room --
# exactly the clipped-anti-aliased-edge failure mode this padding exists to
# avoid. At 4px there is a small, clean margin with the full glyph
# comfortably inside. At 10px the margin is visibly more whitespace than
# needed. 4 is the smallest padding that reliably cleared every glyph edge
# checked.
FORMULA_CROP_PADDING_PX = 4


def formula_region_area_pt2(region: dict) -> float:
    """Bbox area in PDF points^2 (before PAGE_IMAGES_SCALE).

    Returns 0.0 for a region with no bbox -- `_formula_regions` can produce
    one (its `bbox` field is `None` when the source `prov` had no bbox);
    such a region is never transcribable.
    """
    bbox = region.get("bbox")
    if not bbox:
        return 0.0
    width = (bbox.get("r") or 0) - (bbox.get("l") or 0)
    height = (bbox.get("t") or 0) - (bbox.get("b") or 0)
    return max(width, 0.0) * max(height, 0.0)


def is_transcribable(region: dict) -> bool:
    """Whether `region` clears `MIN_FORMULA_REGION_AREA_PT2` and is
    therefore worth a dedicated VLM transcription call."""
    return formula_region_area_pt2(region) >= MIN_FORMULA_REGION_AREA_PT2


def crop_formula_region(
    page_image_bytes: bytes,
    region: dict,
    *,
    images_scale: float = PAGE_IMAGES_SCALE,
    padding_px: int = FORMULA_CROP_PADDING_PX,
) -> bytes:
    """Crop `region`'s bbox out of `page_image_bytes`, returning PNG bytes.

    `page_image_bytes` must be the *same page's* rendered PNG that
    `region["page_no"]` refers to (as produced by
    `docling_wrapper._page_images`, one entry per page).

    Converts `region["bbox"]` (in PDF points) to pixel coordinates at
    `images_scale` -- defaulting to `PAGE_IMAGES_SCALE`, the exact value
    `DoclingParser` actually renders page images at, so a caller never needs
    to (and should never) pass a second, independently-hardcoded scale that
    could drift out of sync with the real render call.

    `region["coordinate_origin"] == "BOTTOMLEFT"` (the only value observed
    against real PDFs so far) means `bbox["t"]`/`bbox["b"]` are measured
    from the *bottom* of the page -- flipped here to top-left pixel
    coordinates via `region["page_height"] - value`. Any other or missing
    coordinate_origin is treated as already top-left (no flip): Docling's
    own alternative, never observed in practice for a real formula region,
    but handled rather than assumed impossible.

    A small pixel padding (`padding_px`, `FORMULA_CROP_PADDING_PX` by
    default) is added on every side so a tightly-fit bbox doesn't clip
    anti-aliased glyph edges (verified by eye -- see that constant's
    docstring), then the whole box is clamped to the image's actual pixel
    bounds so a region flush against (or padding that would push past) a
    page edge can never produce an out-of-range crop.
    """
    from PIL import Image

    bbox = region["bbox"]
    page_height = region["page_height"]
    origin = region.get("coordinate_origin")

    image = Image.open(BytesIO(page_image_bytes))
    image.load()

    left_pt, right_pt = bbox["l"], bbox["r"]
    if origin == "BOTTOMLEFT":
        top_pt = page_height - bbox["t"]
        bottom_pt = page_height - bbox["b"]
    else:
        top_pt, bottom_pt = bbox["t"], bbox["b"]

    left = left_pt * images_scale - padding_px
    top = top_pt * images_scale - padding_px
    right = right_pt * images_scale + padding_px
    bottom = bottom_pt * images_scale + padding_px

    left = max(0.0, min(left, image.width))
    top = max(0.0, min(top, image.height))
    right = max(0.0, min(right, image.width))
    bottom = max(0.0, min(bottom, image.height))

    # Defensive only -- a real region's bbox always has r > l and t > b (see
    # `formula_region_area_pt2`), so this shouldn't trigger in practice, but
    # a degenerate/malformed one must never produce a zero/negative-size box
    # that PIL's `.crop` would silently mishandle.
    if right <= left:
        right = min(float(image.width), left + 1.0)
    if bottom <= top:
        bottom = min(float(image.height), top + 1.0)

    box = (int(round(left)), int(round(top)), int(round(right)), int(round(bottom)))
    cropped = image.crop(box)
    buffer = BytesIO()
    cropped.save(buffer, format="PNG")
    return buffer.getvalue()
