"""Dimension-aware, perceptual screenshot comparison and heatmap rendering."""

from __future__ import annotations

import math
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageStat


def compare_images(
    reference_path: str,
    comparison_path: str,
    diff_path: str | None = None,
    ignore_regions: list[dict[str, int]] | None = None,
) -> dict[str, Any]:
    """Compare equal-size images without concealing layout errors through resizing."""
    with Image.open(reference_path) as source:
        reference = source.convert("RGB")
    with Image.open(comparison_path) as source:
        comparison = source.convert("RGB")

    dimension_match = reference.size == comparison.size
    if not dimension_match:
        if diff_path:
            canvas = Image.new(
                "RGB",
                (max(reference.width, comparison.width), max(reference.height, comparison.height)),
                "#660000",
            )
            canvas.paste(reference, (0, 0))
            ImageDraw.Draw(canvas).text((8, 8), "DIMENSION MISMATCH", fill="red")
            canvas.save(diff_path)
        return {
            "similarity_score": 0.0,
            "perceptual_score": 0.0,
            "changed_pixel_percent": 100.0,
            "dimension_match": False,
            "reference_size": reference.size,
            "comparison_size": comparison.size,
        }

    # Ignored regions are replaced with the reference pixels in the comparison,
    # which keeps dynamic status bars or timestamps from influencing the score.
    for region in ignore_regions or []:
        box = (
            int(region.get("left", 0)),
            int(region.get("top", 0)),
            int(region.get("right", 0)),
            int(region.get("bottom", 0)),
        )
        if not (0 <= box[0] < box[2] <= reference.width and 0 <= box[1] < box[3] <= reference.height):
            raise ValueError(f"Invalid ignored visual region: {region}")
        comparison.paste(reference.crop(box), box)

    difference = ImageChops.difference(reference, comparison)
    channel_means = ImageStat.Stat(difference).mean
    average_difference = sum(channel_means) / len(channel_means)
    pixel_score = max(0.0, 100.0 * (1.0 - average_difference / 255.0))

    gray_difference = difference.convert("L")
    rms = math.sqrt(sum(value * value for value in ImageStat.Stat(gray_difference).rms) / 1)
    perceptual_score = max(0.0, 100.0 * (1.0 - rms / 255.0))
    histogram = gray_difference.histogram()
    changed_pixels = sum(histogram[21:])
    changed_percent = 100.0 * changed_pixels / max(1, reference.width * reference.height)
    combined_score = 0.4 * pixel_score + 0.6 * perceptual_score

    if diff_path:
        intensity = ImageEnhance.Contrast(gray_difference).enhance(3.0)
        red = Image.new("RGB", reference.size, "red")
        annotated = Image.blend(reference, red, 0.0)
        annotated.paste(red, mask=intensity)
        annotated.save(diff_path)

    return {
        "similarity_score": round(combined_score, 2),
        "pixel_similarity_score": round(pixel_score, 2),
        "perceptual_score": round(perceptual_score, 2),
        "average_pixel_difference": round(average_difference, 2),
        "changed_pixel_percent": round(changed_percent, 2),
        "dimension_match": True,
        "reference_size": reference.size,
        "comparison_size": comparison.size,
        "ignored_regions": ignore_regions or [],
    }
