"""Private object storage. Browser uploads go to staging, never research originals."""
from __future__ import annotations

from pathlib import Path


class LocalObjects:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def path(self, key):
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root):
            raise ValueError("Invalid object key")
        return path

    def put(self, key, data, mime):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Content-addressed, immutable research objects. Never overwrite a file.
        try:
            with path.open('xb') as output:
                output.write(data)
            path.chmod(0o600)
        except FileExistsError:
            if path.read_bytes() != data:
                raise ValueError("Object content changed")

    def read(self, key, limit):
        with self.path(key).open('rb') as source:
            data = source.read(limit + 1)
        if len(data) > limit:
            raise ValueError("Object exceeds size limit")
        return data

    def delete(self, key):
        self.path(key).unlink(missing_ok=True)
