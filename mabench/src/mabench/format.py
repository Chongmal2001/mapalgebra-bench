"""Byte layout of the tile store.

This module is the single source of truth for the binary layout shared by the
Python tooling, NES and the GeoTrellis baseline (``MapletIO``). Any change
here must be mirrored in both readers and bumps ``RECORD_VERSION``.

Frame file  = FRAME_HEADER (16 B), then ``n_records`` records.
Record      = RECORD_HEADER (32 B): the scalar fields of an NES tuple,
              then the payload: exactly one NES ``MapletCodec`` binary blob,
              so NES can hand it to ``Raster::decode`` without re-encoding.

MapletCodec binary blob (``WireFormat::Binary``, see MapletCodec.cpp):

    offset  size  field
    0       1     format    = 2 (WireFormat::Binary)
    1       1     cell type = 1 (CellType::Float64)
    2       1     bands     (1..255; all bands of the tile, band-major)
    3       1     reserved, 0
    4       4     u32 height (rows)      <- height before width
    8       4     u32 width  (cols)
    12      4     reserved, 0
    16      8     f64 noData sentinel (NaN in every benchmark store)
    24      8·b·h·w f64 cells, band-major, row-major within a band, little-endian

``decode`` rejects a blob whose bands, height, width or noData differ from the
stream's ``TileSpec``, so every tile has the same shape (edge tiles are padded
with NaN) and the TileSpec must carry ``bands = <store bands>`` and
``noData = NaN``.

Every offset is a multiple of 8 (16 + k·(32 + 24 + 8·h·w)), so readers can
view cells in place (mmap / DoubleBuffer / reinterpret_cast).
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

import numpy as np

RECORD_VERSION = 3

FRAME_MAGIC = b"MPLFRM03"
# magic, frame_id, n_records
FRAME_HEADER = struct.Struct("<8sII")
# event_time_ms, frame_id, bands, flags, tile_x, tile_y, cols, rows
RECORD_HEADER = struct.Struct("<QIHHiiII")
# MapletCodec binary preamble: format, cell type, bands, pad, height, width, pad(4), noData
BLOB_PREAMBLE = struct.Struct("<BBBxIIxxxxd")

WIRE_FORMAT_BINARY = 2
CELL_TYPE_FLOAT64 = 1
MAX_BANDS = 255  # the band count is one byte in the blob preamble
NODATA = float("nan")

CELL_DTYPE = np.dtype("<f8")

assert FRAME_HEADER.size == 16
assert RECORD_HEADER.size == 32
assert BLOB_PREAMBLE.size == 24


@dataclass(frozen=True)
class RecordHeader:
    event_time_ms: int
    frame_id: int
    bands: int
    flags: int
    tile_x: int
    tile_y: int
    cols: int
    rows: int

    @property
    def cell_count(self) -> int:
        return self.bands * self.rows * self.cols

    @property
    def payload_nbytes(self) -> int:
        """Size of the MapletCodec blob that follows the header."""
        return BLOB_PREAMBLE.size + self.cell_count * CELL_DTYPE.itemsize

    def pack(self) -> bytes:
        return RECORD_HEADER.pack(
            self.event_time_ms, self.frame_id, self.bands, self.flags,
            self.tile_x, self.tile_y, self.cols, self.rows,
        )

    @classmethod
    def unpack_from(cls, buf, offset: int = 0) -> "RecordHeader":
        return cls(*RECORD_HEADER.unpack_from(buf, offset))


def pack_frame_header(frame_id: int, n_records: int) -> bytes:
    return FRAME_HEADER.pack(FRAME_MAGIC, frame_id, n_records)


def unpack_frame_header(buf) -> tuple[int, int]:
    magic, frame_id, n_records = FRAME_HEADER.unpack_from(buf, 0)
    if magic != FRAME_MAGIC:
        raise ValueError(f"not a frame file (magic {magic!r}, expected {FRAME_MAGIC!r})")
    return frame_id, n_records


def encode_blob(cells: np.ndarray) -> bytes:
    """(bands, rows, cols) or (rows, cols) float64 -> MapletCodec binary blob, band-major, NaN noData."""
    if cells.ndim == 2:
        cells = cells[None]
    if cells.ndim != 3:
        raise ValueError(f"expected (bands, rows, cols), got shape {cells.shape}")
    bands, rows, cols = cells.shape
    if not 1 <= bands <= MAX_BANDS:
        raise ValueError(f"band count {bands} outside 1..{MAX_BANDS}")
    preamble = BLOB_PREAMBLE.pack(WIRE_FORMAT_BINARY, CELL_TYPE_FLOAT64, bands, rows, cols, NODATA)
    return preamble + np.ascontiguousarray(cells, dtype=CELL_DTYPE).tobytes()


def decode_blob(buf, offset: int, bands: int, rows: int, cols: int) -> np.ndarray:
    """Validate a blob's preamble like ``Raster::decode`` does; return a (bands, rows, cols) view."""
    fmt_, cell_type, blob_bands, height, width, nodata = BLOB_PREAMBLE.unpack_from(buf, offset)
    if fmt_ != WIRE_FORMAT_BINARY:
        raise ValueError(f"blob wire format {fmt_} != {WIRE_FORMAT_BINARY} (binary)")
    if cell_type != CELL_TYPE_FLOAT64:
        raise ValueError(f"blob cell type {cell_type} != {CELL_TYPE_FLOAT64} (float64)")
    if (blob_bands, height, width) != (bands, rows, cols):
        raise ValueError(f"blob is {blob_bands}x{height}x{width} (bands x rows x cols), "
                         f"record header says {bands}x{rows}x{cols}")
    if not math.isnan(nodata):
        raise ValueError(f"blob noData is {nodata}; benchmark stores use NaN")
    cells = np.frombuffer(buf, dtype=CELL_DTYPE, count=bands * rows * cols, offset=offset + BLOB_PREAMBLE.size)
    return cells.reshape(bands, rows, cols)


def iter_records(buf):
    """Yield (header, cells) for every record in a frame file buffer.

    ``cells`` is a read-only (bands, rows, cols) view into ``buf``; copy it if it
    must outlive the buffer.
    """
    _, n_records = unpack_frame_header(buf)
    offset = FRAME_HEADER.size
    for _ in range(n_records):
        header = RecordHeader.unpack_from(buf, offset)
        offset += RECORD_HEADER.size
        yield header, decode_blob(buf, offset, header.bands, header.rows, header.cols)
        offset += header.payload_nbytes
    if offset != len(buf):
        raise ValueError(f"frame file has {len(buf) - offset} trailing bytes")
