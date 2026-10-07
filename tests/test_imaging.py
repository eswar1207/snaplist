"""Image steps without the model: validation, mask cleaning, composition, compliance."""

import io

import numpy as np
import pytest
from PIL import Image

from snaplist import imaging
from tests.photos import product_photo, to_jpeg


def test_rejects_small_images():
    with pytest.raises(imaging.InvalidImageError, match="shortest side"):
        imaging.inspect_image(to_jpeg(np.zeros((300, 800, 3), np.uint8)))


def test_rejects_unsupported_format():
    buffer = io.BytesIO()
    Image.new("RGB", (800, 800)).save(buffer, format="GIF")
    with pytest.raises(imaging.InvalidImageError, match="not allowed"):
        imaging.inspect_image(buffer.getvalue())


def test_rejects_bytes_that_are_not_an_image():
    with pytest.raises(imaging.InvalidImageError, match="cannot read"):
        imaging.inspect_image(b"this is not a photo")


def test_decode_turns_phone_photos_upright():
    # Orientation 6 means "rotate 90 degrees": a 1200x900 photo should become 900x1200.
    image, _ = product_photo(1200, 900)
    decoded = imaging.decode_image(to_jpeg(image, exif_orientation=6))
    assert decoded.shape[:2] == (1200, 900)


def test_clean_mask_drops_small_blobs_and_background_haze():
    mask = np.full((600, 600), 5, np.uint8)  # faint haze everywhere
    mask[100:400, 100:400] = 255  # the product
    mask[500:505, 500:505] = 255  # a tiny false spot
    cleaned = imaging.clean_mask(mask)
    assert cleaned[200, 200] == 255
    assert cleaned[502, 502] == 0
    assert cleaned[50, 50] == 0  # haze removed, so the background will be pure white


def test_main_image_meets_amazon_rules():
    image, mask = product_photo()
    composite = imaging.compose_on_background(image, mask, imaging.white_canvas(2000))
    final = imaging.decode_jpeg(imaging.encode_jpeg(composite.image))
    report = imaging.check_main_image(final, composite)
    assert report.passed, report.checks
    assert report.measurements["product_fill_ratio"] == pytest.approx(0.85, abs=0.005)
    assert report.measurements["background_white_ratio"] == 1.0
    assert final.shape == (2000, 2000, 3)


def test_check_catches_a_non_white_background():
    image, mask = product_photo()
    grey = np.full((2000, 2000, 3), 240, np.uint8)
    composite = imaging.compose_on_background(image, mask, grey)
    report = imaging.check_main_image(composite.image, composite)
    assert not report.passed
    assert report.checks["background_pure_white"] is False


def test_empty_mask_means_no_product():
    image, _ = product_photo()
    with pytest.raises(imaging.InvalidImageError, match="no product"):
        imaging.compose_on_background(image, np.zeros(image.shape[:2], np.uint8), imaging.white_canvas(1000))


def test_check_catches_a_product_that_is_too_small():
    image, mask = product_photo()
    composite = imaging.compose_on_background(image, mask, imaging.white_canvas(2000), fill=0.6)
    report = imaging.check_main_image(composite.image, composite)
    assert report.checks["product_fills_85_percent_or_more"] is False
    assert report.measurements["product_fill_ratio"] == pytest.approx(0.6, abs=0.005)
