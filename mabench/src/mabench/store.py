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


class TileStore:
    def __init__(self, backend: StorageBackend, manifest: Manifest):
        self.backend, self.manifest = backend, manifest

    @classmethod
    def open(cls, location) -> "TileStore":
        backend = backend_for(location)
        return cls(backend, Manifest.load(backend))

    def records(self, frame_id: int) -> Iterator[tuple[fmt.RecordHeader, np.ndarray]]:
        """(header, cells) per tile. Cells are copies, safe to keep."""
        buf = self.backend.read_buffer(self.manifest.frame(frame_id).key)
        try:
            for header, cells in fmt.iter_records(buf):
                yield header, cells.copy()
        finally:
            _close(buf)

    def keys(self, frame_id: int) -> list[tuple[int, int]]:
        """(tile_x, tile_y) of every record, in file order, without copying any cells."""
        buf = self.backend.read_buffer(self.manifest.frame(frame_id).key)
        try:
            return [(h.tile_x, h.tile_y) for h, _ in fmt.iter_records(buf)]
        finally:
            _close(buf)

    def assemble(self, frame_id: int) -> np.ndarray:
        """Whole frame as (bands, height, width), padding cropped away. Needs the whole frame in memory;
        use ``assemble_rows`` to work through large frames strip by strip."""
        return self.assemble_rows(frame_id, 0, self.manifest.raster.height)

    def assemble_rows(self, frame_id: int, row0: int, row1: int) -> np.ndarray:
        """Rows [row0, row1) of a frame as (bands, row1 - row0, width); only the tiles overlapping those rows
        are decoded, so memory is proportional to the strip, not the frame. Missing tiles stay NaN."""
        m = self.manifest
        s, H, W = m.tiling.tile_size, m.raster.height, m.raster.width
        if not 0 <= row0 < row1 <= H:
            raise ValueError(f"rows [{row0}, {row1}) outside raster height {H}")
        out = np.full((m.raster.bands, row1 - row0, m.tiling.tiles_x * s), np.nan)
        ty0, ty1 = row0 // s, (row1 - 1) // s
        buf = self.backend.read_buffer(m.frame(frame_id).key)
        try:
            for h, cells in fmt.iter_records(buf):          # cells are views; only overlapping rows are copied
                if not ty0 <= h.tile_y <= ty1:
                    continue
                t0 = h.tile_y * s
                a, b = max(row0, t0), min(row1, t0 + s)
                out[:, a - row0:b - row0, h.tile_x * s:(h.tile_x + 1) * s] = cells[:, a - t0:b - t0, :]
        finally:
            _close(buf)
        return out[:, :, :W]

    def verify(self) -> VerifyReport:
        """Checksums, record counts, tile grid completeness, header consistency, padding."""
        m, report = self.manifest, VerifyReport()
        s, bands = m.tiling.tile_size, m.raster.bands
        expected_keys = {(x, y) for y in range(m.tiling.tiles_y) for x in range(m.tiling.tiles_x)}
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
                fid, n = fmt.unpack_frame_header(buf)
                if fid != info.frame_id or n != info.n_records:
                    report.problems.append(f"{where}: frame header ({fid}, {n}) disagrees with manifest")
                seen, valid = set(), 0
                for h, cells in fmt.iter_records(buf):
                    if (h.frame_id, h.event_time_ms, h.bands, h.cols, h.rows) != (info.frame_id, info.event_time_ms, bands, s, s):
                        report.problems.append(f"{where}: bad header at tile ({h.tile_x},{h.tile_y})")
                    key = (h.tile_x, h.tile_y)
                    if key in seen:
                        report.problems.append(f"{where}: duplicate tile {key}")
                    seen.add(key)
                    vr = min(s, m.raster.height - h.tile_y * s)
                    vc = min(s, m.raster.width - h.tile_x * s)
                    if vr > 0 and vc > 0:
                        valid += int(np.count_nonzero(~np.isnan(cells[:, :vr, :vc])))
                    padding = np.ones((s, s), bool)
                    padding[:max(vr, 0), :max(vc, 0)] = False
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
