from __future__ import annotations

import json
from collections import Counter
from functools import lru_cache

from PIL import Image, ImageEnhance, ImageOps

from palette import PALETTE, rgb_to_oklab
from presets import get_custom_preset

RESAMPLING = getattr(Image, "Resampling", Image)

# Pastel sources (low source chroma) are the failure case for plain OKLab:
# the palette only has "light gray-pinks" (P8/Re8/Am8) as bright options, which
# win on lightness but look washed out. In that regime we down-weight lightness
# and up-weight hue (a,b) so that a saturated-but-darker same-hue entry (e.g. P4)
# beats both the gray-of-same-hue (P8) and the hue-shifted-neighbor (Am8).
# For already-saturated sources the plain OKLab metric works fine and lightness
# must be respected, so the conditional branch keeps both regimes correct.
_CHROMA_CAP = 0.08
_PASTEL_LIGHTNESS_WEIGHT = 0.3
_PASTEL_CHROMA_WEIGHT = 1.0
# Hue weight is high to prevent drift into neighboring hue groups (e.g. pastel
# pink leaking into Or8 / Am8). Undersaturation penalty is moderate so that very
# bright highlights can still match the palette's only "very light" entry in
# that hue family (e.g. P8 for near-white pink), yielding natural shading
# gradients instead of flattening all pastels onto P4.
_PASTEL_HUE_WEIGHT = 20.0
_PASTEL_UNDERSAT_PENALTY = 40.0
# Sources below this chroma threshold are treated as "effectively neutral" —
# no undersaturation penalty, so a pure-white source matches B5 instead of P8.
_NEAR_GRAY_CHROMA_FLOOR = 0.008
# JPEG/photographed sources tend to compress chroma, so a very faint pink tint
# (avg chroma ~0.014 on real inputs) collapses onto B5 "pure white" because the
# pink tint is mathematically below any reasonable threshold. A mild pre-boost
# recovers the tint the eye sees without over-saturating already-vivid colors.
_SOURCE_SATURATION_BOOST = 1.3


def convert_dot_snapshot(payload_json: str) -> str:
    payload = json.loads(payload_json)
    width, height = get_custom_preset(
        payload["ratio"],
        int(payload["precision"]),
        payload.get("canvas_width"),
        payload.get("canvas_height"),
    )

    @lru_cache(maxsize=65536)
    def nearest_palette_color(red: int, green: int, blue: int) -> dict[str, object]:
        lightness, axis_a, axis_b = rgb_to_oklab(red, green, blue)
        source_chroma = (axis_a * axis_a + axis_b * axis_b) ** 0.5
        is_pastel_source = source_chroma < _CHROMA_CAP

        best_score: float | None = None
        best_color: dict[str, object] | None = None
        for candidate in PALETTE:
            palette_l, palette_a, palette_b = candidate["oklab"]
            d_lightness_sq = (lightness - palette_l) ** 2
            d_a = axis_a - palette_a
            d_b = axis_b - palette_b
            d_ab_sq = d_a * d_a + d_b * d_b
            if is_pastel_source:
                # Decompose into ΔC (chroma magnitude) and ΔH (hue angle) so
                # hue can be weighted independently. ΔH² = Δa² + Δb² - ΔC².
                d_chroma = candidate["oklab_chroma"] - source_chroma
                d_chroma_sq = d_chroma * d_chroma
                d_hue_sq = d_ab_sq - d_chroma_sq
                if d_hue_sq < 0:
                    d_hue_sq = 0.0  # floating-point guard
                if source_chroma > _NEAR_GRAY_CHROMA_FLOOR and d_chroma < 0:
                    penalty = _PASTEL_UNDERSAT_PENALTY * d_chroma_sq
                else:
                    penalty = 0.0
                score = (
                    _PASTEL_LIGHTNESS_WEIGHT * d_lightness_sq
                    + _PASTEL_CHROMA_WEIGHT * d_chroma_sq
                    + _PASTEL_HUE_WEIGHT * d_hue_sq
                    + penalty
                )
            else:
                score = d_lightness_sq + d_ab_sq
            if best_score is None or score < best_score:
                best_score = score
                best_color = candidate
        return best_color

    with Image.open(payload["path"]) as original:
        corrected = ImageOps.exif_transpose(original)
        image = corrected.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        composed = Image.alpha_composite(background, image).convert("RGB")
        if _SOURCE_SATURATION_BOOST != 1.0:
            composed = ImageEnhance.Color(composed).enhance(_SOURCE_SATURATION_BOOST)
        # The browser crop box already defines the exact framing.
        # Resize directly so the selected area is preserved without a second crop.
        # Use NEAREST when source matches or is close to target size to avoid
        # introducing anti-aliased intermediate colors (e.g. grays in pure B&W art).
        src_w, src_h = composed.size
        if src_w == width and src_h == height:
            fitted = composed
        elif abs(src_w - width) <= 2 and abs(src_h - height) <= 2:
            fitted = composed.resize((width, height), resample=RESAMPLING.NEAREST)
        else:
            fitted = composed.resize((width, height), resample=RESAMPLING.LANCZOS)
        source_pixels = list(fitted.getdata())

    usage: Counter[str] = Counter()
    grid_codes: list[list[str]] = []

    for row_index in range(height):
        row_codes: list[str] = []
        start = row_index * width
        for column_index in range(width):
            red, green, blue = source_pixels[start + column_index]
            color = nearest_palette_color(red, green, blue)
            row_codes.append(str(color["code"]))
            usage[str(color["code"])] += 1
        grid_codes.append(row_codes)

    used_colors = [
        {
            "code": color["code"],
            "group": color["group"],
            "hex_value": color["hex_value"],
            "count": usage[str(color["code"])],
        }
        for color in PALETTE
        if usage[str(color["code"])] > 0
    ]

    return json.dumps(
        {
            "width": width,
            "height": height,
            "used_colors": used_colors,
            "grid_codes": grid_codes,
        },
        ensure_ascii=False,
    )
