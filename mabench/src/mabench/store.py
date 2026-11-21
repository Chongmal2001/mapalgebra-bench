"""Read and verify tile stores."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Iterator

import numpy as np

from . import format as fmt
from .manifest import Manifest
from .storage import StorageBackend, backend_for


def _close(buf) -> None:
    """Close an mmap; if numpy views still reference it, leave it to the garbage collector."""
    if hasattr(buf, "close"):
        try:
            buf.close()
        except BufferError:
            pass


@dataclass
class VerifyReport:
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


class FrameRows:
    """One frame, its maplets indexed by tile row once, so each strip touches only its own tiles.
    Cells stay views into the frame buffer until ``close``."""

    def __init__(self, store: "TileStore", frame_id: int):
        m = store.manifest
        self.bands, self.s, self.H, self.W = m.raster.bands, m.tiling.tile_size, m.raster.height, m.raster.width
        self.tiles_x = m.tiling.tiles_x
        self._buf = store.backend.read_buffer(m.frame(frame_id).key)
        self.keys: list[tuple[int, int]] = []
        self._rows: dict[int, list[tuple[int, np.ndarray]]] = {}
        for h, cells in fmt.iter_maplets(self._buf):
            if h.flags & fmt.FLAG_LATE:      # belongs to an earlier frame
                continue
            self.keys.append((h.tile_x, h.tile_y))
            self._rows.setdefault(h.tile_y, []).append((h.tile_x, cells))

    def rows(self, row0: int, row1: int) -> np.ndarray:
        s, H = self.s, self.H
        if not 0 <= row0 < row1 <= H:
            raise ValueError(f"rows [{row0}, {row1}) outside raster height {H}")
        out = np.full((self.bands, row1 - row0, self.tiles_x * s), np.nan)
        for ty in range(row0 // s, (row1 - 1) // s + 1):
            t0 = ty * s
            a, b = max(row0, t0), min(row1, t0 + s)
            for tx, cells in self._rows.get(ty, ()):     # file order, so a later duplicate wins as before
                out[:, a - row0:b - row0, tx * s:(tx + 1) * s] = cells[:, a - t0:b - t0, :]
        return out[:, :, :self.W]

    def close(self) -> None:
        self._rows.clear()
        _close(self._buf)

    def __enter__(self) -> "FrameRows":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class TileStore:
    def __init__(self, backend: StorageBackend, manifest: Manifest, location=None):
        self.backend, self.manifest, self.location = backend, manifest, location

    @classmethod
    def open(cls, location) -> "TileStore":
        backend = backend_for(location)
        return cls(backend, Manifest.load(backend), str(location))

    def frame_rows(self, frame_id: int) -> FrameRows:
        return FrameRows(self, frame_id)

    def frame_header(self, frame_id: int) -> fmt.FrameHeader:
        buf = self.backend.read_buffer(self.manifest.frame(frame_id).key)
        try:
            return fmt.unpack_frame_header(buf)
        finally:
            _close(buf)

    def records(self, frame_id: int) -> Iterator[tuple[fmt.MapletHeader, np.ndarray]]:
        """(maplet header, cells) per tile, in file order. Cells are copies, safe to keep."""
        buf = self.backend.read_buffer(self.manifest.frame(frame_id).key)
        try:
            for header, cells in fmt.iter_maplets(buf):
                yield header, cells.copy()
        finally:
            _close(buf)

    def keys(self, frame_id: int) -> list[tuple[int, int]]:
        """(tile_x, tile_y) of every record, in file order, without copying any cells."""
        buf = self.backend.read_buffer(self.manifest.frame(frame_id).key)
        try:
            return [(h.tile_x, h.tile_y) for h, _ in fmt.iter_maplets(buf)]
        finally:
            _close(buf)

    def assemble(self, frame_id: int) -> np.ndarray:
        """Whole frame as (bands, height, width), padding cropped away. Needs the whole frame in memory;
        use ``assemble_rows`` to work through large frames strip by strip."""
        return self.assemble_rows(frame_id, 0, self.manifest.raster.height)

    def assemble_rows(self, frame_id: int, row0: int, row1: int) -> np.ndarray:
        """Rows [row0, row1) of a frame as (bands, row1 - row0, width); only the tiles overlapping those rows
        are copied, so memory is proportional to the strip, not the frame. Missing tiles stay NaN.
        For many strips of one frame use ``frame_rows`` (indexes the frame once)."""
        with self.frame_rows(frame_id) as f:
            return f.rows(row0, row1)

    def verify(self) -> VerifyReport:
        """Checksums, frame header vs manifest, maplet headers, tile grid completeness, valid cells, padding."""
        m, report = self.manifest, VerifyReport()
        s, bands = m.tiling.tile_size, m.raster.bands
        expected_keys = {(x, y) for y in range(m.tiling.tiles_y) for x in range(m.tiling.tiles_x)}
        epsg = fmt.crs_to_epsg(m.raster.crs)
        transform = tuple(m.raster.transform) if m.raster.transform is not None else fmt.IDENTITY_TRANSFORM
        for info in m.frames:
            where = f"frame {info.frame_id}"
            if not self.backend.exists(info.key):
                report.problems.append(f"{where}: missing object {info.key}")
                continue
            buf = self.backend.read_buffer(info.key)
            try:
                if hashlib.sha256(buf).hexdigest() != info.sha256:
                    report.problems.append(f"{where}: sha256 mismatch")
                    continue
                fh = fmt.unpack_frame_header(buf)
                got = (fh.frame_id, fh.n_tiles, fh.event_time_ms, fh.raster_width, fh.raster_height,
                       fh.tile_width, fh.tile_height, fh.bands, fh.crs_epsg, fh.transform)
                want = (info.frame_id, info.n_records, info.event_time_ms, m.raster.width, m.raster.height,
                        s, s, bands, epsg, transform)
                for name, g, w in zip(("frame_id", "n_tiles", "event_time_ms", "raster_width", "raster_height",
                                       "tile_width", "tile_height", "bands", "crs_epsg", "transform"), got, want):
                    if g != w:
                        report.problems.append(f"{where}: frame header {name} {g} != manifest {w}")
                seen, valid = set(), 0
                for h, cells in fmt.iter_maplets(buf):
                    key = (h.tile_x, h.tile_y)
                    if h.flags == fmt.FLAG_LATE:
                        if h.frame_id >= info.frame_id:
                            report.problems.append(f"{where}: late maplet {key} names frame {h.frame_id}, not an earlier one")
                        continue
                    if h.frame_id != 0:
                        report.problems.append(f"{where}: maplet {key} has frame_id {h.frame_id}, files carry 0")
                    if h.flags != 0:
                        report.problems.append(f"{where}: maplet {key} has flags {h.flags:#x}")
                    if key in seen:
                        report.problems.append(f"{where}: duplicate tile {key}")
                    seen.add(key)
                    vr, vc = h.valid_rows, h.valid_cols
                    valid += int(np.count_nonzero(~np.isnan(cells[:, :vr, :vc])))
                    padding = np.ones((s, s), bool)
                    padding[:vr, :vc] = False
                    if not np.all(np.isnan(cells[:, padding])):
                        report.problems.append(f"{where}: padding of tile {key} is not NoData")
                if seen != expected_keys:
                    report.problems.append(f"{where}: {len(expected_keys - seen)} tiles missing, "
                                           f"{len(seen - expected_keys)} unexpected")
                if valid != info.valid_cells:
                    report.problems.append(f"{where}: valid cell count {valid} != manifest {info.valid_cells}")
            except ValueError as exc:
                report.problems.append(f"{where}: {exc}")
            finally:
                _close(buf)
        return report
