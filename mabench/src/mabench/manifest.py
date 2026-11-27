from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from . import format as fmt
from .storage import StorageBackend

MANIFEST_KEY = "manifest.json"
STORE_FORMAT = "mabench-tilestore"
STORE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class RasterInfo:
    width: int
    height: int
    bands: int
    transform: tuple[float, float, float, float, float, float] | None
    crs: str | None


@dataclass(frozen=True)
class TilingInfo:
    tile_size: int
    tiles_x: int
    tiles_y: int
    tile_order: str = "row-major"
    edge_padding: str = "nan"


@dataclass(frozen=True)
class FrameInfo:
    frame_id: int
    event_time_ms: int
    key: str
    n_records: int
    nbytes: int
    sha256: str
    valid_cells: int
    source: dict[str, Any] = field(default_factory=dict)


@dataclass
class Manifest:
    raster: RasterInfo
    tiling: TilingInfo
    frames: list[FrameInfo]
    provenance: dict[str, Any] = field(default_factory=dict)
    cells: dict[str, str] = field(default_factory=lambda: {
        "dtype": "float64", "byte_order": "little", "nodata": "nan",
        "layout": "band-major; row-major within band",
        "payload": "frame header (112 B, MPLFRM04) + NES MapletCodec format-3 maplets (32-byte self-locating preamble, frame_id 0); see docs/maplet-format.md section 5",
    })
    format: str = STORE_FORMAT
    format_version: int = STORE_FORMAT_VERSION
    record_version: int = fmt.RECORD_VERSION

    def frame(self, frame_id: int) -> FrameInfo:
        for f in self.frames:
            if f.frame_id == frame_id:
                return f
        raise KeyError(f"no frame {frame_id}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Manifest":
        if d.get("format") != STORE_FORMAT:
            raise ValueError(f"not a tile store manifest (format={d.get('format')!r})")
        if d.get("record_version") != fmt.RECORD_VERSION:
            raise ValueError(f"record version {d.get('record_version')} != {fmt.RECORD_VERSION}; "
                             "prepare the store again with this mabench version")
        raster = dict(d["raster"])
        if raster.get("transform") is not None:
            raster["transform"] = tuple(raster["transform"])
        return cls(
            raster=RasterInfo(**raster),
            tiling=TilingInfo(**d["tiling"]),
            frames=[FrameInfo(**f) for f in d["frames"]],
            provenance=d.get("provenance", {}),
            cells=d["cells"],
            format=d["format"],
            format_version=d["format_version"],
            record_version=d["record_version"],
        )

    def save(self, backend: StorageBackend) -> None:
        backend.write_text(MANIFEST_KEY, json.dumps(self.to_dict(), indent=2) + "\n")

    @classmethod
    def load(cls, backend: StorageBackend) -> "Manifest":
        return cls.from_dict(json.loads(backend.read_text(MANIFEST_KEY)))
