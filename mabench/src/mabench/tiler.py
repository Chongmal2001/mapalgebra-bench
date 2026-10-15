"""Cut a raster source into fixed-size tiles and write a tile store.

Reads one strip of tile rows at a time, so memory stays at roughly
``width * tile_size * bands * 8`` bytes regardless of raster size.
"""

from __future__ import annotations

import hashlib
import math
from typing import Any, Callable

import numpy as np

from . import format as fmt
from .manifest import FrameInfo, Manifest, TilingInfo
from .sources import RasterSource
from .storage import StorageBackend


def frame_key(frame_id: int) -> str:
    return f"frames/f{frame_id:06d}.mpl"


def tile_source(source: RasterSource, backend: StorageBackend, tile_size: int,
                provenance: dict[str, Any] | None = None,
                progress: Callable[[str], None] | None = None) -> Manifest:
    if tile_size <= 0:
        raise ValueError("tile_size must be positive")
    g = source.geometry
    if not 1 <= g.bands <= fmt.MAX_BANDS:
        raise ValueError(f"source has {g.bands} bands; a maplet carries 1..{fmt.MAX_BANDS}")
    tiles_x, tiles_y = math.ceil(g.width / tile_size), math.ceil(g.height / tile_size)
    n_records = tiles_x * tiles_y
    frames: list[FrameInfo] = []

    for frame in source.frames():
        key = frame_key(frame.frame_id)
        digest, nbytes, valid = hashlib.sha256(), 0, 0
        with backend.open_write(key) as fh, frame.open() as reader:
            def emit(chunk: bytes) -> None:
                nonlocal nbytes
                fh.write(chunk)
                digest.update(chunk)
                nbytes += len(chunk)

            emit(fmt.pack_frame_header(frame.frame_id, n_records))
            for ty in range(tiles_y):
                row0 = ty * tile_size
                nrows = min(tile_size, g.height - row0)
                strip = reader.read(row0, 0, nrows, g.width)
                if strip.shape != (g.bands, nrows, g.width):
                    raise ValueError(f"reader returned {strip.shape}, expected {(g.bands, nrows, g.width)}")
                valid += int(np.count_nonzero(~np.isnan(strip)))
                for tx in range(tiles_x):
                    col0 = tx * tile_size
                    ncols = min(tile_size, g.width - col0)
                    tile = np.full((g.bands, tile_size, tile_size), np.nan, dtype=fmt.CELL_DTYPE)
                    tile[:, :nrows, :ncols] = strip[:, :, col0:col0 + ncols]
                    header = fmt.RecordHeader(frame.event_time_ms, frame.frame_id, g.bands, 0,
                                              tx, ty, tile_size, tile_size)
                    emit(header.pack())
                    emit(fmt.encode_blob(tile))
        frames.append(FrameInfo(frame.frame_id, frame.event_time_ms, key, n_records, nbytes,
                                digest.hexdigest(), valid, frame.description))
        if progress:
            progress(f"frame {frame.frame_id}: {n_records} tiles, {nbytes / 2**20:.1f} MiB")

    if not frames:
        raise ValueError("source produced no frames")
    manifest = Manifest(
        raster=g,
        tiling=TilingInfo(tile_size=tile_size, tiles_x=tiles_x, tiles_y=tiles_y),
        frames=frames,
        provenance={"source": source.describe(), **(provenance or {})},
    )
    manifest.save(backend)
    return manifest
