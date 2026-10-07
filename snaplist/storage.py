"""File storage for original photos and generated outputs.

Locally this is a folder. In production it would be Amazon S3 (same key layout),
with a CDN in front for downloads. Writes are atomic: we write a temporary file
and rename it, so a reader never sees a half-written image, and a retried job
simply overwrites the same keys (idempotent).

Key layout:
    originals/<job_id>.<ext>
    outputs/<job_id>/<file name>
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError(f"key escapes storage root: {key}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp_name, path)  # atomic on the same file system
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def path(self, key: str) -> Path:
        return self._path(key)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


def original_key(job_id: str, extension: str) -> str:
    return f"originals/{job_id}{extension}"


def output_key(job_id: str, name: str) -> str:
    return f"outputs/{job_id}/{name}"
