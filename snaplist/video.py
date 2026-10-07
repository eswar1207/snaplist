"""A short "B-roll" product clip made from one still image.

It uses the Ken Burns effect: a slow zoom-in with a gentle pan, eased at the
start and end so it feels smooth. Encoded as H.264 MP4 with the ffmpeg binary
that ships inside the imageio-ffmpeg package (no system install needed).
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np


def _smoothstep(t: float) -> float:
    return t * t * (3 - 2 * t)


def make_broll(image_rgb: np.ndarray, seconds: float = 5.0, fps: int = 24, size: int = 1080,
               zoom: float = 0.12, pan: float = 0.03) -> bytes:
    height, width = image_rgb.shape[:2]
    frame_count = max(2, int(seconds * fps))
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "broll.mp4"
        writer = imageio_ffmpeg.write_frames(
            str(path),
            (size, size),
            fps=fps,
            codec="libx264",
            pix_fmt_out="yuv420p",  # plays on every phone and browser
            macro_block_size=8,  # 1080 is divisible by 8, so no resizing
            output_params=["-crf", "23", "-preset", "veryfast", "-movflags", "+faststart"],
        )
        writer.send(None)  # start the ffmpeg process
        try:
            for index in range(frame_count):
                ease = _smoothstep(index / (frame_count - 1))
                window = width / (1.0 + zoom * ease)  # the part of the image shown
                center_x = width / 2 + width * pan * (ease - 0.5)
                center_y = height / 2 - height * pan * 0.5 * ease
                scale = size / window
                matrix = np.float32([
                    [scale, 0, -(center_x - window / 2) * scale],
                    [0, scale, -(center_y - window / 2) * scale],
                ])
                frame = cv2.warpAffine(image_rgb, matrix, (size, size), flags=cv2.INTER_LINEAR,
                                       borderMode=cv2.BORDER_REPLICATE)
                writer.send(np.ascontiguousarray(frame))
        finally:
            writer.close()
        return path.read_bytes()
