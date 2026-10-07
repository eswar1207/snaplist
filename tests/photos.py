"""Synthetic product photos with a known product mask (ground truth) for tests."""

from __future__ import annotations

import io

import cv2
import numpy as np
from PIL import Image


def product_photo(width: int = 1200, height: int = 900, seed: int = 0) -> tuple[np.ndarray, np.ndarray]:
    """A blue bottle standing on a wooden table in front of a wall.

    Returns (rgb image, ground-truth mask with 255 where the bottle is).
    """
    rng = np.random.default_rng(seed)
    image = np.zeros((height, width, 3), dtype=np.float32)
    horizon = int(height * 0.62)
    # Wall: light gradient. Table: warm wood tones with grain stripes.
    wall = np.linspace(205, 170, horizon, dtype=np.float32)[:, None]
    image[:horizon] = np.stack([wall, wall * 0.97, wall * 0.9], axis=-1)
    rows = height - horizon
    grain = 120 + 25 * np.sin(np.linspace(0, 40, width))[None, :] + rng.normal(0, 6, (rows, width))
    image[horizon:] = np.stack([grain * 1.25, grain * 0.85, grain * 0.55], axis=-1)

    mask = np.zeros((height, width), dtype=np.uint8)
    cx = width // 2 + int(rng.integers(-width // 10, width // 10))
    body_w, body_h = int(width * 0.16), int(height * 0.42)
    bottom = horizon + int(rows * 0.45)
    top = bottom - body_h
    cv2.rectangle(mask, (cx - body_w // 2, top), (cx + body_w // 2, bottom), 255, -1)
    neck_w, neck_h = body_w // 3, int(body_h * 0.28)
    cv2.rectangle(mask, (cx - neck_w // 2, top - neck_h), (cx + neck_w // 2, top + 5), 255, -1)
    cv2.ellipse(mask, (cx, top), (body_w // 2, body_w // 5), 0, 180, 360, 255, -1)  # shoulders
    cap_h = neck_h // 3
    cap = np.zeros_like(mask)
    cv2.rectangle(cap, (cx - neck_w // 2 - 4, top - neck_h - cap_h), (cx + neck_w // 2 + 4, top - neck_h), 255, -1)
    mask = np.maximum(mask, cap)

    # Shade the bottle: darker at the sides, a bright highlight stripe.
    xs = np.arange(width, dtype=np.float32)
    shade = 0.55 + 0.45 * np.cos(np.clip((xs - cx) / (body_w / 2), -1, 1) * np.pi / 2)
    bottle = np.stack([20 + 30 * shade, 60 + 70 * shade, 150 + 90 * shade], axis=-1)[None, :, :].repeat(height, 0)
    highlight = np.abs(xs - (cx - body_w * 0.22)) < body_w * 0.05
    bottle[:, highlight] = np.array([235.0, 240.0, 250.0])
    bottle[cap > 0] = np.array([30.0, 30.0, 35.0])
    image = np.where(mask[..., None] > 0, bottle, image)
    image = np.clip(image + rng.normal(0, 3, image.shape), 0, 255).astype(np.uint8)
    return image, mask


def to_jpeg(image_rgb: np.ndarray, quality: int = 92, exif_orientation: int | None = None) -> bytes:
    img = Image.fromarray(image_rgb)
    buffer = io.BytesIO()
    if exif_orientation is None:
        img.save(buffer, format="JPEG", quality=quality)
    else:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation  # Orientation tag
        img.save(buffer, format="JPEG", quality=quality, exif=exif)
    return buffer.getvalue()


def photo_bytes(seed: int = 0, width: int = 1200, height: int = 900) -> bytes:
    return to_jpeg(product_photo(width, height, seed)[0])
