"""Byte layout of the tile store, record version 4 (self-locating maplets).

This module is the Python source of truth for the bytes shared by the tiler, NES (raster plugin,
``MapletCodec`` wire format 3) and the GeoTrellis baseline (``tilestore/Format.scala``). The contract is
``docs/maplet-format.md`` section 5; any change here must be mirrored there, in Scala and in the NES codec.

A frame file (``frames/fNNNNNN.mpl``) is one frame header block followed by ``n_tiles`` maplet blocks::

    frame header, 112 B, struct <8sIIQIIIIBBHIdII6d
    +0    char[8] magic          "MPLFRM04"
    +8    u32     frame_id       file sequence number (mabench bookkeeping; NES ignores it)
    +12   u32     n_tiles        tiles_x * tiles_y
    +16   u64     event_time_ms  one event time for the whole frame
    +24   u32     raster_width   W, true extent before padding
    +28   u32     raster_height  H
    +32   u32     tile_width     T_w
    +36   u32     tile_height    T_h
    +40   u8      bands
    +41   u8      cellType       1 = Float64
    +42   u16     flags          0
    +44   u32     reserved       0
    +48   f64     noData         NaN; the only NoData field in the file
    +56   u32     crs_epsg       0 = unknown
    +60   u32     reserved       0
    +64   f64[6]  transform      affine a, b, c, d, e, f (rasterio / GDAL order)

    maplet, 32 B preamble + cells, struct <BBBBIiiIIII
    +0    u8   format          3
    +1    u8   cellType        1 = Float64
    +2    u8   bands           1..255
    +3    u8   flags           bit 0 = FILLED (tile synthesised by an operator, not observed)
    +4    u32  frame_id        0 in files = unassigned; the NES source assigns it
    +8    i32  tile_x
    +12   i32  tile_y
    +16   u32  height          T_h
    +20   u32  width           T_w
    +24   u32  raster_width    W
    +28   u32  raster_height   H
    +32   f64[bands*height*width] cells, band-major, row-major within a band; padding = NaN

Maplet i starts at ``112 + i * (32 + 8 * bands * T_h * T_w)``; every offset is a multiple of 8, so cells can be
viewed in place (mmap, ``DoubleBuffer``, ``reinterpret_cast``). Maplets carry no NoData: interpretation comes from
the raster level (frame header, manifest).
"""

from __future__ import annotations

import math
import re
import struct
from dataclasses import dataclass, replace
from typing import Iterator, Sequence

import numpy as np

RECORD_VERSION = 4
FRAME_MAGIC = b"MPLFRM04"
FRAME_HEADER = struct.Struct("<8sIIQIIIIBBHIdII6d")
MAPLET_PREAMBLE = struct.Struct("<BBBBIiiIIII")
assert FRAME_HEADER.size == 112 and MAPLET_PREAMBLE.size == 32

MAPLET_FORMAT = 3                    # NES WireFormat::BinaryLocated
CELL_TYPE_FLOAT64 = 1
MAX_BANDS = 255
NODATA = float("nan")
CELL_DTYPE = np.dtype("<f8")
FLAG_FILLED = 0x01                   # maplet synthesised by an operator (NoData fill), not observed data
FLAG_LATE = 0x02                     # tile stores only: maplet of an earlier frame (named by frame_id), arriving late
IDENTITY_TRANSFORM = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)


def crs_to_epsg(crs: str | None) -> int:
    """EPSG code of a CRS string, 0 if there is none or it has no EPSG code."""
    if not crs:
        return 0
    m = re.fullmatch(r"\s*EPSG:(\d+)\s*", crs, re.I)
    if m:
        return int(m.group(1))
    try:                                         # WKT / PROJ strings: let rasterio find the code
        from rasterio.crs import CRS
        return CRS.from_user_input(crs).to_epsg() or 0
    except Exception:
        return 0


# ---------------------------------------------------------------- frame header

@dataclass(frozen=True)
class FrameHeader:
    frame_id: int
    n_tiles: int
    event_time_ms: int
    raster_width: int
    raster_height: int
    tile_width: int
    tile_height: int
    bands: int
    crs_epsg: int = 0
    transform: tuple[float, float, float, float, float, float] = IDENTITY_TRANSFORM
    nodata: float = NODATA
    cell_type: int = CELL_TYPE_FLOAT64
    flags: int = 0

    @property
    def tiles_x(self) -> int:
        return math.ceil(self.raster_width / self.tile_width)

    @property
    def tiles_y(self) -> int:
        return math.ceil(self.raster_height / self.tile_height)

    @property
    def maplet_nbytes(self) -> int:
        return MAPLET_PREAMBLE.size + CELL_DTYPE.itemsize * self.bands * self.tile_height * self.tile_width

    def maplet_offset(self, i: int) -> int:
        return FRAME_HEADER.size + i * self.maplet_nbytes

    @property
    def file_nbytes(self) -> int:
        return self.maplet_offset(self.n_tiles)

    def maplet_header(self, tile_x: int, tile_y: int, frame_id: int = 0, flags: int = 0) -> "MapletHeader":
        """Preamble of a maplet of this frame at (tile_x, tile_y)."""
        return MapletHeader(self.bands, tile_x, tile_y, self.tile_height, self.tile_width,
                            self.raster_width, self.raster_height, frame_id, flags)

    def pack(self) -> bytes:
        if not 1 <= self.bands <= MAX_BANDS:
            raise ValueError(f"band count {self.bands} outside 1..{MAX_BANDS}")
        if len(self.transform) != 6:
            raise ValueError("transform needs 6 coefficients")
        return FRAME_HEADER.pack(FRAME_MAGIC, self.frame_id, self.n_tiles, self.event_time_ms,
                                 self.raster_width, self.raster_height, self.tile_width, self.tile_height,
                                 self.bands, self.cell_type, self.flags, 0, self.nodata, self.crs_epsg, 0,
                                 *(float(v) for v in self.transform))

    @classmethod
    def unpack_from(cls, buf) -> "FrameHeader":
        if len(buf) < FRAME_HEADER.size:
            raise ValueError(f"frame file of {len(buf)} bytes is shorter than its {FRAME_HEADER.size}-byte header")
        (magic, fid, n, ts, W, H, tw, th, bands, ctype, flags, _r0, nodata, epsg, _r1,
         *transform) = FRAME_HEADER.unpack_from(buf, 0)
        if magic != FRAME_MAGIC:
            raise ValueError(f"bad frame magic {magic!r} (expected {FRAME_MAGIC!r}; "
                             "stores written before record version 4 must be prepared again)")
        if ctype != CELL_TYPE_FLOAT64:
            raise ValueError(f"frame cellType {ctype} is not Float64")
        if not math.isnan(nodata):
            raise ValueError(f"frame noData {nodata} is not NaN")
        if not (W and H and tw and th and bands):
            raise ValueError("frame header has a zero dimension")
        h = cls(fid, n, ts, W, H, tw, th, bands, epsg, tuple(transform), nodata, ctype, flags)
        if n > h.tiles_x * h.tiles_y:     # fewer is legal framing (an output that lost tiles); verify reports it
            raise ValueError(f"frame header says {n} tiles, grid {h.tiles_x}x{h.tiles_y} has {h.tiles_x * h.tiles_y}")
        return h


def unpack_frame_header(buf) -> FrameHeader:
    return FrameHeader.unpack_from(buf)


# ---------------------------------------------------------------- maplet

@dataclass(frozen=True)
class MapletHeader:
    bands: int
    tile_x: int
    tile_y: int
    height: int
    width: int
    raster_width: int
    raster_height: int
    frame_id: int = 0
    flags: int = 0

    @property
    def cell_count(self) -> int:
        return self.bands * self.height * self.width

    @property
    def nbytes(self) -> int:
        return MAPLET_PREAMBLE.size + CELL_DTYPE.itemsize * self.cell_count

    @property
    def valid_rows(self) -> int:
        """Rows of this tile inside the raster extent (the rest is padding)."""
        return max(0, min(self.height, self.raster_height - self.tile_y * self.height))

    @property
    def valid_cols(self) -> int:
        return max(0, min(self.width, self.raster_width - self.tile_x * self.width))

    def pack(self) -> bytes:
        if not 1 <= self.bands <= MAX_BANDS:
            raise ValueError(f"band count {self.bands} outside 1..{MAX_BANDS}")
        return MAPLET_PREAMBLE.pack(MAPLET_FORMAT, CELL_TYPE_FLOAT64, self.bands, self.flags, self.frame_id,
                                    self.tile_x, self.tile_y, self.height, self.width,
                                    self.raster_width, self.raster_height)

    @classmethod
    def unpack_from(cls, buf, offset: int = 0) -> "MapletHeader":
        if len(buf) - offset < MAPLET_PREAMBLE.size:
            raise ValueError(f"truncated maplet preamble at offset {offset}")
        fmt_, ctype, bands, flags, fid, tx, ty, h, w, W, H = MAPLET_PREAMBLE.unpack_from(buf, offset)
        if fmt_ != MAPLET_FORMAT:
            raise ValueError(f"maplet at offset {offset}: format {fmt_}, expected {MAPLET_FORMAT}")
        if ctype != CELL_TYPE_FLOAT64:
            raise ValueError(f"maplet at offset {offset}: cellType {ctype} is not Float64")
        if bands == 0:
            raise ValueError(f"maplet at offset {offset}: zero bands")
        return cls(bands, tx, ty, h, w, W, H, fid, flags)


def encode_maplet(header: MapletHeader, cells: np.ndarray) -> bytes:
    """Preamble + cells. ``cells`` is (bands, height, width), or (height, width) for one band."""
    cells = np.asarray(cells, dtype=CELL_DTYPE)
    if cells.ndim == 2:
        cells = cells[None]
    if cells.shape != (header.bands, header.height, header.width):
        raise ValueError(f"cells {cells.shape} do not match maplet shape "
                         f"{(header.bands, header.height, header.width)}")
    return header.pack() + np.ascontiguousarray(cells).tobytes()


def decode_maplet(buf, offset: int = 0) -> tuple[MapletHeader, np.ndarray]:
    """(header, cells) of the maplet at ``offset``; cells are a read-only view into ``buf``."""
    h = MapletHeader.unpack_from(buf, offset)
    if len(buf) - offset < h.nbytes:
        raise ValueError(f"maplet at offset {offset} needs {h.nbytes} bytes, {len(buf) - offset} left")
    cells = np.frombuffer(buf, CELL_DTYPE, h.cell_count, offset + MAPLET_PREAMBLE.size)
    return h, cells.reshape(h.bands, h.height, h.width)


def check_maplet(frame: FrameHeader, h: MapletHeader) -> None:
    """The source checks of maplet-format.md 5.5 that need no cell decoding (duplicates excepted)."""
    if (h.bands, h.height, h.width, h.raster_width, h.raster_height) != \
            (frame.bands, frame.tile_height, frame.tile_width, frame.raster_width, frame.raster_height):
        raise ValueError(f"maplet ({h.tile_x},{h.tile_y}) shape {(h.bands, h.height, h.width)} / raster "
                         f"{(h.raster_width, h.raster_height)} disagrees with the frame header")
    if not (0 <= h.tile_x < frame.tiles_x and 0 <= h.tile_y < frame.tiles_y):
        raise ValueError(f"maplet position ({h.tile_x},{h.tile_y}) outside grid {frame.tiles_x}x{frame.tiles_y}")


def iter_maplets(buf) -> Iterator[tuple[MapletHeader, np.ndarray]]:
    """(maplet header, cells view) for every maplet of a frame file, checked against the frame header.
    The frame header itself is ``unpack_frame_header(buf)``."""
    frame = FrameHeader.unpack_from(buf)
    if len(buf) != frame.file_nbytes:
        kind = "trailing bytes" if len(buf) > frame.file_nbytes else "truncated"
        raise ValueError(f"frame file is {len(buf)} bytes, header implies {frame.file_nbytes} ({kind})")
    for i in range(frame.n_tiles):
        h, cells = decode_maplet(buf, frame.maplet_offset(i))
        check_maplet(frame, h)
        yield h, cells


def encode_frame(frame: FrameHeader, tiles: Sequence[tuple[MapletHeader, np.ndarray]]) -> bytes:
    """A whole frame file in memory (tests, small stores). The tiler streams instead."""
    if len(tiles) != frame.n_tiles:
        frame = replace(frame, n_tiles=len(tiles))
    return frame.pack() + b"".join(encode_maplet(h, c) for h, c in tiles)
