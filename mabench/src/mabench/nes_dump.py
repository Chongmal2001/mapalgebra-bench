"""Read a NES ``Binary`` dump and turn it into a candidate output store (``mabench import-nes``).

The dump is what the NES File sink writes with output format ``Binary`` for the sink schema
``(srcId UINT32, srcFrameId UINT32, ts UINT64, raster VARSIZED)``, all NOT NULL (``docs/maplet-format.md`` §8)::

    text line   DUMP_HEADER_LINE (the File sink's schema line; NES upper-cases unquoted names), then until end of file:
    +0   u32  srcId         lineage: input store (RasterFile SRC_ID)
    +4   u32  srcFrameId    lineage: frame header frame_id of the input frame
    +8   u64  ts            event time of the input frame
    +16  u32  len           maplet length = 32 + 8 * B * T_h * T_w
    +20  u8[len] maplet     format-3 maplet: frame_id = focal output frame number (from 1), position, flags, cells

Records arrive in emission order, not sorted by frame or tile. The importer reads the dump twice: a first pass indexes
every record (offset and preamble only) and runs all checks, a second pass copies the cells frame by frame into the
output store, so memory is proportional to the number of records, not to the dump size.

Mapping back to the input: records are grouped by the maplet ``frame_id`` (one group = one focal output frame); a group
maps through ``srcFrameId`` to the input frame of the same ``frame_id`` and is written under that id, so ``compare``
matches frames. ``ts`` is a cross-check against the input frame's event time. Maplets are written with ``frame_id 0``
and ``flags 0`` as every store file requires; the FILLED flag is kept in the provenance instead.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass, field, replace
from typing import Any, BinaryIO, Callable

import numpy as np

from . import format as fmt
from .manifest import FrameInfo, Manifest
from .storage import backend_for
from .store import TileStore
from .tiler import frame_key

DUMP_HEADER_LINE = (b"srcId:UINT32:NOT_NULLABLE,srcFrameId:UINT32:NOT_NULLABLE,"
                    b"ts:UINT64:NOT_NULLABLE,raster:VARSIZED:NOT_NULLABLE\n")
DUMP_RECORD = struct.Struct("<IIQI")
assert DUMP_RECORD.size == 20


@dataclass(frozen=True)
class DumpRecord:
    offset: int                 # of the record (its 20-byte head) in the dump
    src_id: int
    src_frame_id: int
    ts: int
    maplet: fmt.MapletHeader

    @property
    def maplet_offset(self) -> int:
        return self.offset + DUMP_RECORD.size

    @property
    def key(self) -> tuple[int, int]:
        return self.maplet.tile_x, self.maplet.tile_y


@dataclass
class ImportReport:
    """What ``import_nes`` found besides hard errors; also recorded in the output provenance."""
    out_frame_of: dict[int, int] = field(default_factory=dict)          # input frame id -> focal output frame no
    filled_tiles: dict[int, list[list[int]]] = field(default_factory=dict)
    missing_tiles: dict[int, list[list[int]]] = field(default_factory=dict)
    duplicates_dropped: int = 0
    padding_not_nodata: dict[int, list[list[int]]] = field(default_factory=dict)
    frames_without_output: list[int] = field(default_factory=list)
    records: int = 0

    def notes(self) -> list[str]:
        out = []
        for fid, tiles in self.missing_tiles.items():
            out.append(f"frame {fid}: {len(tiles)} tile(s) missing from the dump (written as a partial frame), "
                       f"e.g. {tiles[:3]}")
        for fid, tiles in self.filled_tiles.items():
            out.append(f"frame {fid}: {len(tiles)} tile(s) flagged FILLED by NES, e.g. {tiles[:3]}")
        if self.duplicates_dropped:
            out.append(f"{self.duplicates_dropped} identical duplicate record(s) dropped")
        for fid, tiles in self.padding_not_nodata.items():
            out.append(f"frame {fid}: padding of {len(tiles)} edge tile(s) is not NoData, e.g. {tiles[:3]} "
                       "(kept as written; compare ignores padding, verify reports it)")
        if self.frames_without_output:
            out.append(f"{len(self.frames_without_output)} input frame(s) have no output in the dump: "
                       f"{self.frames_without_output[:10]}")
        return out


def _read_header_line(fh: BinaryIO, path: str) -> int:
    """The File sink's schema line. NES prints unquoted identifiers upper-cased (``Identifier`` formatter), so the field
    names are compared case-insensitively; order, types and nullability must match exactly."""
    line = fh.readline(len(DUMP_HEADER_LINE) + 1)
    if line.upper() != DUMP_HEADER_LINE.upper():
        raise ValueError(f"{path}: first line is {line[:200]!r}, expected the File sink schema line "
                         f"{DUMP_HEADER_LINE.decode().strip()!r} (wrong sink schema, field order or output format; "
                         "the sink must be (srcId, srcFrameId, ts, raster), all NOT NULL, with output format Binary)")
    return len(line)


def index_dump(path: str, maplet_nbytes: int | None = None) -> list[DumpRecord]:
    """Every record of a dump with its maplet preamble, in file order. Checks the header line and the framing; with
    ``maplet_nbytes`` every ``len`` must equal it."""
    records = []
    with open(path, "rb") as fh:
        fh.seek(0, 2)
        size = fh.tell()
        fh.seek(0)
        pos = _read_header_line(fh, path)
        while pos < size:
            head = fh.read(DUMP_RECORD.size)
            if len(head) < DUMP_RECORD.size:
                raise ValueError(f"{path}: truncated record head at offset {pos} ({len(head)} of {DUMP_RECORD.size} bytes)")
            src_id, src_fid, ts, n = DUMP_RECORD.unpack(head)
            if maplet_nbytes is not None and n != maplet_nbytes:
                raise ValueError(f"{path}: record at offset {pos} has a {n}-byte maplet, the input store's maplets are "
                                 f"{maplet_nbytes} bytes")
            if pos + DUMP_RECORD.size + n > size:
                raise ValueError(f"{path}: truncated record at offset {pos}: maplet needs {n} bytes, "
                                 f"{size - pos - DUMP_RECORD.size} left")
            pre = fh.read(fmt.MAPLET_PREAMBLE.size)
            try:
                h = fmt.MapletHeader.unpack_from(pre)
            except ValueError as exc:
                raise ValueError(f"{path}: record at offset {pos}: {exc}") from None
            if h.nbytes != n:
                raise ValueError(f"{path}: record at offset {pos}: len {n} != maplet size {h.nbytes} from its preamble")
            records.append(DumpRecord(pos, src_id, src_fid, ts, h))
            pos += DUMP_RECORD.size + n
            fh.seek(pos)
    return records


def _maplet_bytes(fh: BinaryIO, rec: DumpRecord) -> bytes:
    fh.seek(rec.maplet_offset)
    return fh.read(rec.maplet.nbytes)


def import_nes(dump_path: str, input_store: TileStore, workload, out_location: str, src_id: int | None = None,
               provenance: dict[str, Any] | None = None,
               progress: Callable[[str], None] | None = None) -> tuple[Manifest, ImportReport]:
    """Turn a NES dump into an output store on the input's raster and tiling (``bench-capabilities.md`` §9.1)."""
    if getattr(workload, "output_kind", None) is None or workload.output_kind.value != "raster":
        raise ValueError(f"import-nes needs a workload with raster output (focal), not {workload.name}")
    mi = input_store.manifest
    frames_by_id = {f.frame_id: f for f in mi.frames}
    anchor = input_store.frame_header(mi.frames[0].frame_id)
    records = index_dump(dump_path, anchor.maplet_nbytes)
    report = ImportReport(records=len(records))
    if not records:
        raise ValueError(f"{dump_path}: no records after the header line (did the query produce output?)")

    # ---- per-record checks
    src_ids = {r.src_id for r in records}
    if src_id is not None:
        bad = [r for r in records if r.src_id != src_id]
        if bad:
            raise ValueError(f"record at offset {bad[0].offset} has srcId {bad[0].src_id}, expected {src_id} "
                             f"({len(bad)} such record(s))")
    elif len(src_ids) > 1:
        raise ValueError(f"dump holds records of several sources {sorted(src_ids)}; pick one with --src-id")
    headers: dict[int, fmt.FrameHeader] = {}
    for r in records:
        where = f"record at offset {r.offset}"
        if r.src_frame_id not in frames_by_id:
            raise ValueError(f"{where}: srcFrameId {r.src_frame_id} is not a frame of the input store "
                             f"(frames {sorted(frames_by_id)[:10]}...)")
        if r.src_frame_id not in headers:
            headers[r.src_frame_id] = input_store.frame_header(r.src_frame_id)
        try:
            fmt.check_maplet(headers[r.src_frame_id], r.maplet)
        except ValueError as exc:
            raise ValueError(f"{where}: {exc}") from None
        if r.maplet.frame_id < 1:
            raise ValueError(f"{where}: maplet frame_id {r.maplet.frame_id}; focal numbers its output frames from 1 "
                             "(0 = unassigned is a programming error in NES)")

    # ---- grouping by focal output frame, mapping to input frames
    groups: dict[int, list[DumpRecord]] = {}
    for r in records:
        groups.setdefault(r.maplet.frame_id, []).append(r)
    in_of_out: dict[int, int] = {}
    for out_no, recs in sorted(groups.items()):
        first = recs[0]
        for r in recs:
            if (r.src_frame_id, r.ts) != (first.src_frame_id, first.ts):
                raise ValueError(f"output frame {out_no}: records disagree on (srcFrameId, ts): "
                                 f"{(first.src_frame_id, first.ts)} at offset {first.offset}, "
                                 f"{(r.src_frame_id, r.ts)} at offset {r.offset}")
        want_ts = frames_by_id[first.src_frame_id].event_time_ms
        if first.ts != want_ts:
            raise ValueError(f"output frame {out_no}: ts {first.ts} != event time {want_ts} of input frame "
                             f"{first.src_frame_id}")
        if first.src_frame_id in report.out_frame_of:
            raise ValueError(f"output frames {report.out_frame_of[first.src_frame_id]} and {out_no} both map to input "
                             f"frame {first.src_frame_id} (sliding windows? the dump schema has no window bounds)")
        report.out_frame_of[first.src_frame_id] = out_no
        in_of_out[out_no] = first.src_frame_id
    report.frames_without_output = sorted(set(frames_by_id) - set(report.out_frame_of))

    # ---- placement and writing, one output frame at a time
    backend = backend_for(out_location)
    tiles_x, tiles_y = mi.tiling.tiles_x, mi.tiling.tiles_y
    expected = [(x, y) for y in range(tiles_y) for x in range(tiles_x)]
    s = mi.tiling.tile_size
    infos: list[FrameInfo] = []
    with open(dump_path, "rb") as dump:
        for fid in sorted(in_of_out.values()):
            recs = groups[report.out_frame_of[fid]]
            placed: dict[tuple[int, int], DumpRecord] = {}
            for r in recs:
                if r.key in placed:
                    if _maplet_bytes(dump, r) != _maplet_bytes(dump, placed[r.key]):
                        raise ValueError(f"frame {fid}: tile {r.key} appears twice with different contents "
                                         f"(offsets {placed[r.key].offset} and {r.offset})")
                    report.duplicates_dropped += 1
                    continue
                placed[r.key] = r
            present = [k for k in expected if k in placed]
            missing = [list(k) for k in expected if k not in placed]
            if missing:
                report.missing_tiles[fid] = missing
            filled = [list(k) for k in present if placed[k].maplet.flags & fmt.FLAG_FILLED]
            if filled:
                report.filled_tiles[fid] = filled
            header = replace(headers[fid], n_tiles=len(present))
            key = frame_key(fid)
            digest, nbytes, valid, bad_padding = hashlib.sha256(), 0, 0, []
            with backend.open_write(key) as fh:
                def emit(chunk: bytes) -> None:
                    nonlocal nbytes
                    fh.write(chunk)
                    digest.update(chunk)
                    nbytes += len(chunk)

                emit(header.pack())
                for k in present:
                    r = placed[k]
                    raw = _maplet_bytes(dump, r)
                    h = replace(r.maplet, frame_id=0, flags=0)
                    cells = np.frombuffer(raw, fmt.CELL_DTYPE, h.cell_count, fmt.MAPLET_PREAMBLE.size)
                    cells = cells.reshape(h.bands, h.height, h.width)
                    vr, vc = h.valid_rows, h.valid_cols
                    valid += int(np.count_nonzero(~np.isnan(cells[:, :vr, :vc])))
                    padding = np.ones((s, s), bool)
                    padding[:vr, :vc] = False
                    if not np.all(np.isnan(cells[:, padding])):
                        bad_padding.append(list(k))
                    emit(h.pack() + raw[fmt.MAPLET_PREAMBLE.size:])
            if bad_padding:
                report.padding_not_nodata[fid] = bad_padding
            infos.append(FrameInfo(fid, frames_by_id[fid].event_time_ms, key, len(present), nbytes,
                                   digest.hexdigest(), valid, {"nes_output_frame": report.out_frame_of[fid]}))
            if progress:
                progress(f"frame {fid} (NES output frame {report.out_frame_of[fid]}): {len(present)} of "
                         f"{len(expected)} tiles")

    nes = {"dump": str(dump_path), "src_id": next(iter(src_ids)), "records": report.records,
           "out_frame_of": report.out_frame_of, "filled_tiles": report.filled_tiles,
           "missing_tiles": report.missing_tiles, "duplicates_dropped": report.duplicates_dropped,
           "padding_not_nodata": report.padding_not_nodata, "frames_without_output": report.frames_without_output}
    manifest = Manifest(raster=mi.raster, tiling=mi.tiling, frames=infos,
                        provenance={"role": "nes", "workload": workload.to_dict(),
                                    "input_frames": {f.frame_id: f.sha256 for f in mi.frames},
                                    "input": input_store.backend.location, "nes": nes, **(provenance or {})})
    manifest.save(backend)
    return manifest, report


# --------------------------------------------------------------------------- NES query for a store and workload

NES_OPS = {"sum": "FOCAL_SUM", "mean": "FOCAL_MEAN", "min": "FOCAL_MIN", "max": "FOCAL_MAX"}
NES_SHAPES = {"square": "SQUARE", "circle": "DISC"}          # DISC must be dy^2 + dx^2 <= r^2 like the oracle


DUMP_SCHEMA = (("srcId", "UINT32"), ("srcFrameId", "UINT32"), ("ts", "UINT64"), ("raster", "VARSIZED"))
SOURCE_SCHEMA = (("ts", "UINT64"), ("srcId", "UINT32"), ("srcFrameId", "UINT32"), ("raster", "VARSIZED"))


def nes_query(store: TileStore, workload, dump_path: str | None, source: str = "tiles", src_id: int = 1,
              yaml: bool = False, worker: str = "localhost:8080", data_address: str = "localhost:9090",
              store_path: str | None = None, passthrough: bool = False, frames_per_window: int = 1) -> str:
    """NES statements that run ``workload`` on ``store`` and write the dump ``import-nes`` reads: RasterFile source,
    one frame per tumbling window (window size = smallest event-time step between frames), the 9 positional
    ``FOCAL_*`` arguments from the manifest, File sink with output format Binary and the dump schema.
    ``dump_path=None``: Void sink instead (output discarded; throughput runs).
    ``passthrough=True``: the same source and sink without the focal window (baseline for source and sink cost).
    ``frames_per_window``: tumbling window of that many event-time steps (several frames per window).

    Default: SQL statements for ``nes-repl-embedded``. ``yaml=True``: a topology file for ``nes-cli`` (one worker).
    ``store_path`` overrides the store location as NES sees it (e.g. inside a container)."""
    p, m = workload.params(), store.manifest
    if workload.family != "focal":
        raise ValueError(f"no NES query for workload family {workload.family}")
    times = sorted(f.event_time_ms for f in m.frames)
    steps = [b - a for a, b in zip(times, times[1:]) if b > a]
    if frames_per_window < 1:
        raise ValueError("frames_per_window must be >= 1")
    window = (min(steps) if steps else 1000) * frames_per_window
    r, t = m.raster, m.tiling
    location = store_path or store.backend.location
    location = location if "://" in location or store_path else os.path.abspath(location)
    call = (f"{NES_OPS[p['op']]}(raster, {NES_SHAPES[p['shape']]}, {p['radius']}, NODATA, nan, "
            f"{r.width}, {r.height}, {t.tile_size}, {t.tile_size})")
    select = [
        "SELECT srcId, srcFrameId, ts, raster FROM (",
        f"    SELECT ts, srcId, srcFrameId, {call} AS raster",
        f"    FROM {source} WINDOW TUMBLING(ts, size {window} ms)) INTO dump",
    ]
    if passthrough:
        select = [f"SELECT srcId, srcFrameId, ts, raster FROM {source} INTO dump"]
    if yaml:
        def fields(schema, indent):
            return [f"{indent}- name: {n}\n{indent}  type: {t}" for n, t in schema]
        return "\n".join([
            f"# mabench nes-query: {workload.name} on {location}",
            "query: |", *("  " + line for line in select), "",
            "sinks:", "  - name: dump", f"    host: {worker}", "    schema:", *fields(DUMP_SCHEMA, "      "),
            *(["    type: Void", "    config: {}", ""] if dump_path is None else
              ["    type: File", "    config:", f"      file_path: {dump_path}", "      output_format: Binary", ""]),
            "logical:", f"  - name: {source}", "    schema:", *fields(SOURCE_SCHEMA, "      "), "",
            "physical:", f"  - logical: {source}", f"    host: {worker}", "    parser_config:", "      type: NATIVE",
            "    type: RasterFile", "    source_config:", f"      file_path: {location}", f"      src_id: {src_id}", "",
            "workers:", f"  - host: {worker}", f"    data_address: {data_address}", "    max_operators: 10000", ""])
    if dump_path is None:
        raise ValueError("the Void sink is only emitted as a nes-cli topology (yaml=True)")
    return "\n".join([
        f"CREATE LOGICAL SOURCE {source}(ts UINT64 NOT NULL, srcId UINT32 NOT NULL, srcFrameId UINT32 NOT NULL, "
        "raster VARSIZED NOT NULL);",
        f"CREATE PHYSICAL SOURCE FOR {source} TYPE RasterFile SET('NATIVE' AS INPUT_FORMATTER.\"TYPE\", "
        f"'{location}' AS \"SOURCE\".FILE_PATH, {src_id} AS \"SOURCE\".SRC_ID);",
        "CREATE SINK dump(srcId UINT32 NOT NULL, srcFrameId UINT32 NOT NULL, ts UINT64 NOT NULL, raster VARSIZED NOT NULL) "
        f"TYPE File SET('{dump_path}' AS \"SINK\".FILE_PATH, 'Binary' AS \"SINK\".OUTPUT_FORMAT);",
        *select[:-1], select[-1] + ";",
    ])
