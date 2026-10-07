"""Background-removal model (salient object segmentation) running on ONNX Runtime.

The models are U^2-Net-p and IS-Net, published by their authors under Apache-2.0
and distributed as ONNX files by the rembg project. Pre- and post-processing
follow rembg's reference implementation so the masks match what the model
was trained for.

The original ONNX files have a fixed batch size of 1. `make_batch_dynamic`
rewrites the graph so the batch dimension is symbolic. This lets a GPU
deployment run several images in one call; on CPU we measured no gain
(see BENCHMARKS.md), so the CPU default is batch size 1.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import onnx
import onnxruntime as ort


@dataclass(frozen=True)
class ModelSpec:
    name: str
    file: str
    input_size: int
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    download_url: str


MODEL_SPECS: dict[str, ModelSpec] = {
    "u2netp": ModelSpec(
        name="u2netp",
        file="u2netp.onnx",
        input_size=320,
        mean=(0.485, 0.456, 0.406),
        std=(0.229, 0.224, 0.225),
        download_url="https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2netp.onnx",
    ),
    "isnet-general-use": ModelSpec(
        name="isnet-general-use",
        file="isnet-general-use.onnx",
        input_size=1024,
        mean=(0.5, 0.5, 0.5),
        std=(1.0, 1.0, 1.0),
        download_url="https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
    ),
}


def make_batch_dynamic(src: Path, dst: Path) -> None:
    """Rewrite an ONNX model so its first (batch) dimension can be any size.

    We only change the declared input/output shapes and drop the stored
    intermediate shapes (they assumed batch=1); ONNX Runtime re-infers them.
    The test suite checks that batched outputs equal single-image outputs.
    """
    model = onnx.load(str(src))
    for tensor in list(model.graph.input) + list(model.graph.output):
        dim = tensor.type.tensor_type.shape.dim[0]
        dim.ClearField("dim_value")
        dim.dim_param = "batch"
    del model.graph.value_info[:]
    onnx.save(model, str(dst))


def prepare_model(source: Path) -> Path:
    """Return the batch-dynamic copy of `source`, creating it on first use.

    It is stored next to the original. If that folder is read-only (for example
    a container that runs as a non-root user), it goes to a cache folder in the
    system temp directory instead. The file is written under a temporary name and
    renamed, so two workers starting at the same moment never read half a file.
    """
    for folder in (source.parent, Path(tempfile.gettempdir()) / "snaplist-models"):
        dynamic = folder / f"{source.stem}.dynamic-batch.onnx"
        if dynamic.exists() and dynamic.stat().st_mtime >= source.stat().st_mtime:
            return dynamic
        try:
            folder.mkdir(parents=True, exist_ok=True)
            partial = folder / f"{source.stem}.dynamic-batch.{os.getpid()}.part.onnx"
            make_batch_dynamic(source, partial)
            partial.replace(dynamic)
            return dynamic
        except PermissionError:
            continue
    raise PermissionError(f"cannot write the prepared model for {source}")


class Segmenter:
    """Runs one segmentation model. One instance per worker process."""

    def __init__(self, spec: ModelSpec, model_dir: Path, threads: int = 1) -> None:
        self.spec = spec
        source = model_dir / spec.file
        if not source.exists():
            raise FileNotFoundError(
                f"model file {source} not found; run scripts/download_models.py first"
            )
        dynamic = prepare_model(source)
        options = ort.SessionOptions()
        # One thread per worker process: we scale by adding processes, so they
        # should not fight each other for the same cores.
        options.intra_op_num_threads = threads
        options.inter_op_num_threads = 1
        self._session = ort.InferenceSession(str(dynamic), options, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._output_name = self._session.get_outputs()[0].name

    def preprocess(self, image_rgb: np.ndarray) -> np.ndarray:
        """RGB uint8 image -> normalised float32 tensor of shape (3, S, S)."""
        size = self.spec.input_size
        resized = cv2.resize(image_rgb, (size, size), interpolation=cv2.INTER_AREA).astype(np.float32)
        resized /= max(float(resized.max()), 1.0)  # rembg scales by the image maximum, not 255
        mean = np.array(self.spec.mean, dtype=np.float32)
        std = np.array(self.spec.std, dtype=np.float32)
        normalised = (resized - mean) / std
        return np.ascontiguousarray(normalised.transpose(2, 0, 1))

    def predict(self, images_rgb: list[np.ndarray]) -> list[np.ndarray]:
        """Return one uint8 mask (0..255) per image, each the same size as its image."""
        batch = np.stack([self.preprocess(img) for img in images_rgb])
        raw = self._session.run([self._output_name], {self._input_name: batch})[0][:, 0, :, :]
        masks = []
        for prediction, image in zip(raw, images_rgb):
            low, high = float(prediction.min()), float(prediction.max())
            scaled = (prediction - low) / (high - low) if high > low else np.zeros_like(prediction)
            height, width = image.shape[:2]
            mask = cv2.resize((scaled * 255).astype(np.uint8), (width, height), interpolation=cv2.INTER_LINEAR)
            masks.append(mask)
        return masks


def default_model_dir() -> Path:
    return Path(os.environ.get("SNAPLIST_MODEL_DIR", Path(__file__).resolve().parent.parent / "models"))
