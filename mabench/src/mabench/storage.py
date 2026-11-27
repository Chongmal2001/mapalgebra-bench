from __future__ import annotations

import mmap
import os
import tempfile
from abc import ABC, abstractmethod
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, Iterator


class StorageBackend(ABC):
    location: str

    @abstractmethod
    @contextmanager
    def open_write(self, key: str) -> Iterator[BinaryIO]:
        ...

    @abstractmethod
    def read_buffer(self, key: str):
        ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    def read_text(self, key: str) -> str:
        return bytes(self.read_buffer(key)).decode("utf-8")

    def write_text(self, key: str, text: str) -> None:
        with self.open_write(key) as fh:
            fh.write(text.encode("utf-8"))


class LocalBackend(StorageBackend):
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)
        self.location = str(self.root)

    def _path(self, key: str) -> Path:
        return self.root.joinpath(*key.split("/"))

    @contextmanager
    def open_write(self, key: str):
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                yield fh
            os.replace(tmp, path)  # atomic: no half-written frame
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def read_buffer(self, key: str):
        path = self._path(key)
        if path.stat().st_size == 0:
            return b""
        with open(path, "rb") as fh:
            return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()


class FsspecBackend(StorageBackend):
    def __init__(self, url: str):
        try:
            import fsspec
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise ImportError("object storage needs fsspec: pip install 'mabench[blob]'") from exc
        self.fs, self.root = fsspec.core.url_to_fs(url)
        self.location = url

    def _path(self, key: str) -> str:
        return f"{self.root.rstrip('/')}/{key}"

    @contextmanager
    def open_write(self, key: str):
        path = self._path(key)
        parent = path.rsplit("/", 1)[0]
        self.fs.makedirs(parent, exist_ok=True)
        with self.fs.open(path, "wb") as fh:
            yield fh

    def read_buffer(self, key: str):
        with self.fs.open(self._path(key), "rb") as fh:
            return fh.read()

    def exists(self, key: str) -> bool:
        return self.fs.exists(self._path(key))


def backend_for(location: str | os.PathLike) -> StorageBackend:
    loc = str(location)
    if "://" not in loc:
        return LocalBackend(loc)
    if loc.startswith("file://"):
        return LocalBackend(loc[len("file://"):])
    return FsspecBackend(loc)
