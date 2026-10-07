"""Make the before/after picture for the README from any product photo.

Usage:
    python scripts/make_example.py my_photo.jpg            # your own phone photo
    python scripts/make_example.py                         # a synthetic test photo
    python scripts/make_example.py my_photo.jpg --tier high --out docs/example.jpg
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from snaplist import imaging, pipeline  # noqa: E402
from snaplist.config import TIER_MODELS  # noqa: E402
from snaplist.model import MODEL_SPECS, Segmenter  # noqa: E402

TILE = 560
LABEL_H = 44


def checkerboard(size: int, square: int = 20) -> np.ndarray:
    ys, xs = np.mgrid[0:size, 0:size]
    light = (ys // square + xs // square) % 2 == 0
    return np.repeat(np.where(light[..., None], 235, 205).astype(np.uint8), 3, axis=2)


def fit(image: np.ndarray, size: int, background: np.ndarray | None = None) -> np.ndarray:
    """Scale an RGB or RGBA image to fit inside a size x size tile, centred."""
    tile = background.copy() if background is not None else np.full((size, size, 3), 255, np.uint8)
    h, w = image.shape[:2]
    scale = size / max(h, w)
    resized = cv2.resize(image, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    rh, rw = resized.shape[:2]
    top, left = (size - rh) // 2, (size - rw) // 2
    region = tile[top:top + rh, left:left + rw].astype(np.float32)
    if resized.shape[2] == 4:
        alpha = resized[..., 3:4].astype(np.float32) / 255
        region = resized[..., :3].astype(np.float32) * alpha + region * (1 - alpha)
    else:
        region = resized.astype(np.float32)
    tile[top:top + rh, left:left + rw] = region.astype(np.uint8)
    return tile


def labelled(tile: np.ndarray, text: str) -> np.ndarray:
    label = np.full((LABEL_H, tile.shape[1], 3), 255, np.uint8)
    cv2.putText(label, text, (12, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (40, 40, 40), 2, cv2.LINE_AA)
    return np.vstack([tile, label])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("photo", nargs="?", help="product photo; omit to use a synthetic test photo")
    parser.add_argument("--tier", choices=sorted(TIER_MODELS), default="standard")
    parser.add_argument("--out", default=str(ROOT / "docs" / "example.jpg"))
    args = parser.parse_args()

    if args.photo:
        data = Path(args.photo).read_bytes()
    else:
        from tests.photos import photo_bytes
        data = photo_bytes(seed=3, width=1600, height=1200)
    image = pipeline.prepare(data, max_side=2048)
    segmenter = Segmenter(MODEL_SPECS[TIER_MODELS[args.tier]], ROOT / "models")
    mask = imaging.clean_mask(segmenter.predict([image])[0])
    result = pipeline.render(image, mask, ("cutout", "studio"))
    files = {name: imaging.decode_jpeg(data) for name, data in result.files.items() if name.endswith(".jpg")}
    cutout = cv2.cvtColor(cv2.imdecode(np.frombuffer(result.files["cutout.png"], np.uint8), cv2.IMREAD_UNCHANGED),
                          cv2.COLOR_BGRA2RGBA)

    tiles = [
        labelled(fit(image, TILE, np.full((TILE, TILE, 3), 255, np.uint8)), "Phone photo (input)"),
        labelled(fit(files["main.jpg"], TILE), "Amazon main image" + (" - passed" if result.report["passed"] else "")),
        labelled(fit(files["studio-grey.jpg"], TILE), "Studio shot"),
        labelled(fit(cutout, TILE, checkerboard(TILE)), "Cutout (transparent)"),
    ]
    gap = np.full((TILE + LABEL_H, 12, 3), 255, np.uint8)
    strip = np.hstack([part for tile in tiles for part in (tile, gap)][:-1])
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), cv2.cvtColor(strip, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, 88])
    print(f"wrote {out}  (main image rules passed: {result.report['passed']})")


if __name__ == "__main__":
    main()
