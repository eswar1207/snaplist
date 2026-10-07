"""Image steps around the model: decode, validate, clean the mask, compose, check.

Amazon's main-image rules that this module follows (from Amazon's product image
requirements): pure white background (RGB 255,255,255), the product should fill
about 85% of the frame, and at least 1000 px on the longest side so customers
can zoom. We produce a 2000 x 2000 px square image.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field

import cv2
import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

# MPO is the JPEG-based format some cameras and phones use for multi-frame photos.
ALLOWED_FORMATS = {"JPEG", "MPO", "PNG", "WEBP"}
MIN_SIDE_PX = 500
MAX_PIXELS = 40_000_000  # protects against "decompression bomb" uploads
ALPHA_FLOOR = 20  # mask values below this (out of 255) are background haze, not product
JPEG_EDGE_BAND_PX = 8  # JPEG compression may change pixels this close to the product edge


class InvalidImageError(ValueError):
    """The upload can never be processed (bad format, too small, no product). Not retried."""


@dataclass(frozen=True)
class ImageInfo:
    format: str
    width: int
    height: int


def inspect_image(data: bytes) -> ImageInfo:
    """Cheap check used by the API: reads only the header, not all the pixels."""
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as img:
            info = ImageInfo(format=img.format or "", width=img.width, height=img.height)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise InvalidImageError(f"cannot read image: {exc}") from exc
    if info.format not in ALLOWED_FORMATS:
        raise InvalidImageError(f"format {info.format or 'unknown'} not allowed; use JPEG, PNG or WEBP")
    if info.width * info.height > MAX_PIXELS:
        raise InvalidImageError("image has too many pixels")
    if min(info.width, info.height) < MIN_SIDE_PX:
        raise InvalidImageError(
            f"image is {info.width}x{info.height}; shortest side must be at least {MIN_SIDE_PX} px"
        )
    return info


def decode_image(data: bytes) -> np.ndarray:
    """Bytes -> RGB uint8 array, upright (phone photos store their rotation in EXIF)."""
    inspect_image(data)
    try:
        with Image.open(io.BytesIO(data)) as img:
            rgb = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise InvalidImageError(f"cannot read image: {exc}") from exc
    return np.asarray(rgb)


def clean_mask(mask: np.ndarray, threshold: int = 128, min_component_ratio: float = 0.02) -> np.ndarray:
    """Remove stray blobs and background haze but keep soft edges.

    1. Keep the largest connected region plus any region at least
       `min_component_ratio` of its size (drops small false spots).
    2. Inside the kept regions reuse the model's soft values so edges stay smooth.
    3. Set very low values to 0. The model leaves a faint haze (values 1-19)
       across the background, which would tint the "pure white" background grey.
    """
    binary = (mask >= threshold).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if count <= 1:
        return np.zeros_like(mask)
    areas = stats[1:, cv2.CC_STAT_AREA]
    largest = areas.max()
    keep_labels = [i + 1 for i, area in enumerate(areas) if area >= largest * min_component_ratio]
    keep = np.isin(labels, keep_labels).astype(np.uint8)
    keep = cv2.dilate(keep, np.ones((5, 5), np.uint8)).astype(bool)  # do not cut off the soft edge
    cleaned = np.where(keep, mask, 0).astype(np.uint8)
    cleaned[cleaned < ALPHA_FLOOR] = 0
    return cleaned


def bounding_box(mask: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x0, y0, x1, y1) of the non-zero part of the mask, or None if it is empty."""
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    return int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1


@dataclass
class Composite:
    image: np.ndarray  # RGB uint8, canvas x canvas
    alpha: np.ndarray  # uint8 0..255: where the product sits on the canvas
    product_box: tuple[int, int, int, int]


def compose_on_background(
    image: np.ndarray,
    mask: np.ndarray,
    background: np.ndarray,
    fill: float = 0.85,
    shadow: bool = False,
) -> Composite:
    """Cut the product out and centre it on `background` (a square RGB uint8 image).

    The product is scaled so its longest side is `fill` x canvas size, which is
    how the "product fills 85% of the frame" rule is usually measured.
    Only the area around the product is blended, so the work does not grow
    with the canvas size.
    """
    box = bounding_box(mask)
    if box is None:
        raise InvalidImageError("no product found in the photo")
    x0, y0, x1, y1 = box
    canvas = background.shape[0]
    scale = (fill * canvas) / max(x1 - x0, y1 - y0)
    new_w = max(1, round((x1 - x0) * scale))
    new_h = max(1, round((y1 - y0) * scale))
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_CUBIC
    crop = cv2.resize(image[y0:y1, x0:x1], (new_w, new_h), interpolation=interpolation)
    crop_alpha = cv2.resize(mask[y0:y1, x0:x1], (new_w, new_h), interpolation=cv2.INTER_LINEAR)
    left, top = (canvas - new_w) // 2, (canvas - new_h) // 2

    alpha = np.zeros((canvas, canvas), dtype=np.uint8)
    alpha[top:top + new_h, left:left + new_w] = crop_alpha
    result = background.copy()
    if shadow:
        _add_soft_shadow(result, alpha, (left, top, left + new_w, top + new_h))

    region = result[top:top + new_h, left:left + new_w].astype(np.float32)
    a = crop_alpha.astype(np.float32)[..., None] / 255.0
    blended = crop.astype(np.float32) * a + region * (1.0 - a)
    result[top:top + new_h, left:left + new_w] = np.clip(np.rint(blended), 0, 255).astype(np.uint8)
    return Composite(image=result, alpha=alpha, product_box=(left, top, left + new_w, top + new_h))


def _add_soft_shadow(canvas_rgb: np.ndarray, alpha: np.ndarray, box: tuple[int, int, int, int]) -> None:
    """Darken a blurred, slightly offset copy of the product shape (in place)."""
    size = alpha.shape[0]
    offset = max(1, size // 80)
    blur = (size // 40) * 2 + 1  # odd kernel size
    pad = blur + offset
    left, top, right, bottom = box
    x0, y0 = max(0, left - pad), max(0, top - pad)
    x1, y1 = min(size, right + pad), min(size, bottom + pad)
    shifted = np.zeros((y1 - y0, x1 - x0), dtype=np.float32)
    src = alpha[y0:y1, x0:x1].astype(np.float32) / 255.0
    shifted[offset:, offset // 2:] = src[:-offset, : src.shape[1] - offset // 2]
    shadow = cv2.GaussianBlur(shifted, (blur, blur), 0) * 0.35
    region = canvas_rgb[y0:y1, x0:x1].astype(np.float32)
    canvas_rgb[y0:y1, x0:x1] = np.clip(region * (1.0 - shadow[..., None]), 0, 255).astype(np.uint8)


def white_canvas(size: int = 2000) -> np.ndarray:
    return np.full((size, size, 3), 255, dtype=np.uint8)


def studio_background(size: int, top_rgb: tuple[int, int, int], bottom_rgb: tuple[int, int, int]) -> np.ndarray:
    """A soft vertical gradient with a light vignette, like a photo studio's paper sweep."""
    t = np.linspace(0.0, 1.0, size, dtype=np.float32)[:, None, None]
    top = np.array(top_rgb, dtype=np.float32)[None, None, :]
    bottom = np.array(bottom_rgb, dtype=np.float32)[None, None, :]
    gradient = np.repeat(top * (1 - t) + bottom * t, size, axis=1)
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32) / size - 0.5
    vignette = 1.0 - 0.18 * np.clip((xx**2 + yy**2) * 2.2, 0, 1)
    return np.clip(gradient * vignette[..., None], 0, 255).astype(np.uint8)


STUDIO_STYLES: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "studio-grey": ((246, 246, 246), (200, 202, 206)),
    "studio-warm": ((250, 244, 235), (214, 196, 172)),
}


@dataclass
class ComplianceReport:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    measurements: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks, "measurements": self.measurements}


def check_main_image(final_rgb: np.ndarray, composite: Composite, min_side: int = 1000) -> ComplianceReport:
    """Check the FINAL encoded-and-decoded main image against Amazon's main-image rules.

    `final_rgb` is the JPEG after decoding, because that is what Amazon receives.
    JPEG compression can change pixels right next to the product, so the
    white-background check ignores a thin band (JPEG_EDGE_BAND_PX) around it.
    """
    height, width = final_rgb.shape[:2]
    left, top, right, bottom = composite.product_box
    band = JPEG_EDGE_BAND_PX * 2 + 1
    near_product = cv2.dilate((composite.alpha > 0).astype(np.uint8), np.ones((band, band), np.uint8)) > 0
    background = ~near_product
    pure_white = np.all(final_rgb == 255, axis=2)
    white_ratio = float(pure_white[background].mean()) if background.any() else 0.0
    fill_ratio = max(right - left, bottom - top) / max(width, height)
    touches_edge = left <= 0 or top <= 0 or right >= width or bottom >= height

    checks = {
        "background_pure_white": white_ratio >= 0.999,
        "product_fills_85_percent_or_more": fill_ratio >= 0.848,  # Amazon: 85% or more (2 px rounding slack)
        "longest_side_at_least_1000px": max(width, height) >= min_side,
        "square_1_to_1": width == height,
        "product_not_cut_off": not touches_edge,
    }
    return ComplianceReport(
        passed=all(checks.values()),
        checks=checks,
        measurements={
            "background_white_ratio": round(white_ratio, 5),
            "product_fill_ratio": round(fill_ratio, 4),
            "width_px": float(width),
            "height_px": float(height),
        },
    )


def encode_jpeg(image_rgb: np.ndarray, quality: int = 95) -> bytes:
    """JPEG with full-resolution colour (4:4:4), which keeps edges cleaner on white."""
    params = [
        cv2.IMWRITE_JPEG_QUALITY, quality,
        cv2.IMWRITE_JPEG_SAMPLING_FACTOR, cv2.IMWRITE_JPEG_SAMPLING_FACTOR_444,
    ]
    ok, buffer = cv2.imencode(".jpg", cv2.cvtColor(image_rgb, cv2.COLOR_RGB2BGR), params)
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return buffer.tobytes()


def decode_jpeg(data: bytes) -> np.ndarray:
    return cv2.cvtColor(cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR), cv2.COLOR_BGR2RGB)


def encode_png(image: np.ndarray) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise RuntimeError("PNG encoding failed")
    return buffer.tobytes()
