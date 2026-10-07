"""The ONNX model and the full render pipeline."""

import numpy as np
import pytest

from snaplist import imaging, pipeline
from tests.photos import product_photo


def iou(a: np.ndarray, b: np.ndarray) -> float:
    a, b = a > 127, b > 127
    return float((a & b).sum() / max((a | b).sum(), 1))


def test_batched_model_output_equals_single_image_output(segmenter):
    images = [product_photo(900, 700, seed)[0] for seed in range(3)]
    batched = segmenter.predict(images)
    singles = [segmenter.predict([image])[0] for image in images]
    for together, alone in zip(batched, singles):
        assert np.abs(together.astype(int) - alone.astype(int)).max() <= 1  # rounding only


def test_model_finds_the_product(segmenter):
    image, truth = product_photo(seed=3)
    mask = imaging.clean_mask(segmenter.predict([image])[0])
    assert iou(mask, truth) > 0.8


def test_render_produces_every_output(segmenter):
    image, _ = product_photo(seed=4)
    mask = imaging.clean_mask(segmenter.predict([image])[0])
    result = pipeline.render(image, mask, ("cutout", "studio", "video"))
    assert set(result.files) == {"main.jpg", "studio-grey.jpg", "studio-warm.jpg", "cutout.png", "broll.mp4"}
    assert result.report["passed"], result.report
    assert result.files["broll.mp4"][4:8] == b"ftyp"  # an MP4 file starts with an 'ftyp' box
    assert result.files["cutout.png"][:8] == b"\x89PNG\r\n\x1a\n"


def test_unknown_output_names_are_rejected():
    with pytest.raises(ValueError, match="unknown outputs"):
        pipeline.parse_outputs("studio,hologram")
    assert pipeline.parse_outputs(" video, studio ") == ("studio", "video")


def test_large_photos_are_shrunk_before_processing():
    image, _ = product_photo(4000, 3000)
    from tests.photos import to_jpeg
    prepared = pipeline.prepare(to_jpeg(image), max_side=2048)
    assert max(prepared.shape[:2]) == 2048


def test_prepared_model_goes_to_a_cache_folder_when_the_model_folder_is_read_only(tmp_path, monkeypatch):
    from pathlib import Path
    import shutil
    from snaplist import model

    source = tmp_path / "read-only-models" / "u2netp.onnx"
    source.parent.mkdir()
    shutil.copy(Path(__file__).resolve().parent.parent / "models" / "u2netp.onnx", source)
    real = model.make_batch_dynamic

    def refuse_model_folder(src, dst):
        if dst.parent == source.parent:
            raise PermissionError("read-only file system")
        real(src, dst)

    monkeypatch.setattr(model, "make_batch_dynamic", refuse_model_folder)
    monkeypatch.setattr(model.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    prepared = model.prepare_model(source)
    assert prepared == tmp_path / "tmp" / "snaplist-models" / "u2netp.dynamic-batch.onnx"
    assert model.prepare_model(source) == prepared  # second start reuses it
