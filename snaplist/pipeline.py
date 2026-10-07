"""Turns one photo and its product mask into the output files."""

from __future__ import annotations

import functools
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from . import imaging
from .video import make_broll

EXTRA_OUTPUTS = ("studio", "cutout", "video")  # the Amazon main image is always produced
CANVAS_PX = 2000
STUDIO_FILL = 0.72  # studio shots leave more space around the product than the main image


@dataclass
class RenderResult:
    files: dict[str, bytes]
    report: dict
    timings_ms: dict[str, float] = field(default_factory=dict)


def parse_outputs(value: str) -> tuple[str, ...]:
    """'studio,video' -> ('studio', 'video'). Raises ValueError for unknown names."""
    names = tuple(sorted({part.strip() for part in value.split(",") if part.strip()}))
    unknown = [name for name in names if name not in EXTRA_OUTPUTS]
    if unknown:
        raise ValueError(f"unknown outputs {unknown}; choose from {list(EXTRA_OUTPUTS)}")
    return names


def prepare(data: bytes, max_side: int) -> np.ndarray:
    """Decode and shrink very large photos early: every later step gets cheaper,
    and the output is only 2000 px anyway."""
    image = imaging.decode_image(data)
    height, width = image.shape[:2]
    scale = max_side / max(height, width)
    if scale < 1:
        image = cv2.resize(image, (round(width * scale), round(height * scale)), interpolation=cv2.INTER_AREA)
    return image


@functools.lru_cache(maxsize=8)
def _studio_background(style: str, size: int) -> np.ndarray:
    """Backgrounds never change, so each worker draws each one only once."""
    background = imaging.studio_background(size, *imaging.STUDIO_STYLES[style])
    background.setflags(write=False)  # shared between jobs: must never be modified
    return background


def _cutout_png(image: np.ndarray, mask: np.ndarray) -> bytes:
    """The product on a transparent background (RGBA PNG), cropped to the product."""
    box = imaging.bounding_box(mask)
    if box is None:
        raise imaging.InvalidImageError("no product found in the photo")
    x0, y0, x1, y1 = box
    bgra = cv2.cvtColor(image[y0:y1, x0:x1], cv2.COLOR_RGB2BGRA)
    bgra[..., 3] = mask[y0:y1, x0:x1]
    return imaging.encode_png(bgra)


def render(image: np.ndarray, mask: np.ndarray, extras: tuple[str, ...]) -> RenderResult:
    timings: dict[str, float] = {}
    files: dict[str, bytes] = {}

    started = time.perf_counter()
    main = imaging.compose_on_background(image, mask, imaging.white_canvas(CANVAS_PX))
    files["main.jpg"] = imaging.encode_jpeg(main.image)
    # Check what Amazon will actually receive: the JPEG after compression.
    report = imaging.check_main_image(imaging.decode_jpeg(files["main.jpg"]), main).to_dict()
    timings["main_ms"] = (time.perf_counter() - started) * 1000

    if "cutout" in extras:
        started = time.perf_counter()
        files["cutout.png"] = _cutout_png(image, mask)
        timings["cutout_ms"] = (time.perf_counter() - started) * 1000

    if "studio" in extras or "video" in extras:
        started = time.perf_counter()
        studio_images = {}
        for style in imaging.STUDIO_STYLES:
            shot = imaging.compose_on_background(
                image, mask, _studio_background(style, CANVAS_PX), fill=STUDIO_FILL, shadow=True
            )
            studio_images[style] = shot.image
            if "studio" in extras:
                files[f"{style}.jpg"] = imaging.encode_jpeg(shot.image, quality=90)
        timings["studio_ms"] = (time.perf_counter() - started) * 1000
        if "video" in extras:
            started = time.perf_counter()
            files["broll.mp4"] = make_broll(studio_images["studio-grey"])
            timings["video_ms"] = (time.perf_counter() - started) * 1000

    return RenderResult(files=files, report=report, timings_ms=timings)
