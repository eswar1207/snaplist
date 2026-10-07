"""Download the two background-removal models and verify their SHA-256 checksums.

Both are published under the Apache-2.0 licence by their authors
(U^2-Net: Qin et al. 2020; IS-Net / DIS: Qin et al. 2022) and distributed as
ONNX files by the rembg project.

Usage:  python scripts/download_models.py [--only u2netp]
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from snaplist.model import MODEL_SPECS, prepare_model  # noqa: E402

SHA256 = {
    "u2netp": "309c8469258dda742793dce0ebea8e6dd393174f89934733ecc8b14c76f4ddd8",
    "isnet-general-use": "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a",
}


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default=str(Path(__file__).resolve().parent.parent / "models"))
    parser.add_argument("--only", choices=sorted(MODEL_SPECS), help="download just one model")
    args = parser.parse_args()
    target_dir = Path(args.dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    for name, spec in MODEL_SPECS.items():
        if args.only and name != args.only:
            continue
        path = target_dir / spec.file
        if path.exists() and sha256_of(path) == SHA256[name]:
            prepare_model(path)
            print(f"{name}: already present")
            continue
        print(f"{name}: downloading {spec.download_url}")
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(spec.download_url, tmp)
        actual = sha256_of(tmp)
        if actual != SHA256[name]:
            tmp.unlink()
            sys.exit(f"{name}: checksum mismatch (got {actual}); refusing to use this file")
        tmp.replace(path)
        prepare_model(path)  # done here so a read-only container never has to write it
        print(f"{name}: ok ({path.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
