# Maplet and tile-store format: the NES contract

Authoritative description of the bytes exchanged between the tiler (mabench), NebulaStream (raster plugin) and the
GeoTrellis baseline, and of how they map onto NES tuples and query functions. Every change to the tiler, the
baselines or the NES plugin must agree with this document. When it changes, the change log at the end gets an
entry, and all three codebases plus the golden blobs move together.

Status legend: **[IN FORCE]** is implemented and tested in mabench, the GeoTrellis baseline and the codec patch. **[IN FORCE: tooling]** is implemented and tested in mabench and GeoTrellis, the NES side is pending.
**[SUPERSEDED]** was in force and has been replaced.
**[PROPOSED]** is designed but not decided or implemented. **[OPEN]** is not designed yet.

---

## 0. Vocabulary

| Term | Meaning |
|---|---|
| raster | one georeferenced grid: W × H cells, B bands, float64, NaN = NoData |
| frame | one raster at one point in time; one file `frames/fNNNNNN.mpl`; one NES window |
| tile | a fixed-size T × T piece of a frame at grid position (tile_x, tile_y); edge tiles are padded to T × T with NaN |
| maplet | the encoded form of one tile: NES `MapletCodec` blob = preamble + cells. The unit NES moves as a VARSIZED value |
| tile store | `manifest.json` + `frames/*.mpl`: one prepared dataset |
| manifest | raster-level metadata, once per store |

Metadata lives on three levels. Anything raster-level is stored **once**, never per tile.

| Level | Where | Contains |
|---|---|---|
| raster | `manifest.json` | width, height, bands, transform, CRS, tile size, tiles_x, tiles_y, cell layout, NoData convention, frame list with checksums, provenance |
| frame | frame header (first 112 bytes of each `.mpl`) | frame id, tile count, event time, raster and tile size, bands, NoData, CRS, transform |
| tile | maplet (32-byte preamble + cells) | frame id (assigned by NES), position, shape (bands, height, width), raster size, flags, cells |

---

## 1. Global conventions [IN FORCE]

- **Byte order:** little-endian for every integer and float.
- **Cells:** IEEE-754 float64.
- **NoData:** NaN in every benchmark store. The maplet's `noData` field must be NaN. A NaN cell is NoData regardless of its payload bits.
- **Cell layout:** band-major; within a band row-major (row 0 left to right, then row 1, …). The cell (b, r, c) is at index `b·H·W + r·W + c`.
- **Alignment:** every preamble size is a multiple of 8, so cells can be viewed in place as `double[]`.
- **Tile order in a frame:** row-major by tile (`tile_y` ascending, then `tile_x` ascending).
- **Completeness:** every frame file contains every tile of the grid exactly once (tiles_x · tiles_y tiles). Missing tiles appear only in NES output or in tests.
- **Padding:** every tile is the full T × T. Cells outside the raster extent (right/bottom edge tiles) are NaN. The true extent is `manifest.raster.width/height`.
- **Tile shape is uniform:** every maplet of a store has the same bands, height and width. Consequence: fixed record size, so record *i* starts at a computable offset.

---

## 2. Record format v3 [SUPERSEDED 2026-10-21 by §5; NES CSV systests still use maplet formats 1/2]

`format.py: RECORD_VERSION = 3`, `FRAME_MAGIC = b"MPLFRM03"`.

### 2.1 Frame file

```
offset   size   field
0        8      char[8]  magic        = "MPLFRM03"
8        4      u32      frame_id
12       4      u32      n_records    = tiles_x · tiles_y
16       …      n_records × RECORD
```
struct: `<8sII` (16 B).

### 2.2 Record = tile header (32 B) + maplet

```
TILE HEADER, struct <QIHHiiII, 32 B
+0   u64  event_time_ms     same value for all tiles of a frame
+8   u32  frame_id          = frame header frame_id
+12  u16  bands             = maplet bands       (redundant, consistency check)
+14  u16  flags             = 0                  (reserved)
+16  i32  tile_x            tile column, 0-based
+20  i32  tile_y            tile row, 0-based
+24  u32  cols              = maplet width = T   (redundant, consistency check)
+28  u32  rows              = maplet height = T  (redundant, consistency check)
```

Record size `R = 32 + 24 + 8·B·T·T`; record *i* starts at `16 + i·R`.

### 2.3 Maplet, binary wire format v2 (`format = 2`) [IN FORCE]

This is the exact payload NES `Raster::decode` / `Raster::encode` read and write (MapletCodec `WireFormat::Binary`),
including the multiband patch in `nes/` (byte 2 = band count).

```
PREAMBLE, struct <BBBxIIxxxxd, 24 B
+0   u8   format     = 2   (WireFormat::Binary; byte 0 is the codec's version dispatch byte)
+1   u8   cellType   = 1   (CellType::Float64)
+2   u8   bands      1..255  (before the multiband patch fixed at 1; single-band blobs are byte-identical)
+3   u8   reserved   = 0
+4   u32  height     = T   (height BEFORE width)
+8   u32  width      = T
+12  u32  reserved   = 0
+16  f64  noData     = NaN
CELLS
+24  f64[bands·height·width]   band-major, row-major within a band
```

Maplet size = `24 + 8·B·H·W`. The maplet contains **no position**; position is in the tile header.

### 2.4 Maplet text format v1 (`format = 1`) [IN FORCE in NES only]

The NES codec also has a text format (version byte 1), used by the CSV systests. mabench and GeoTrellis neither
produce nor read it. It is out of scope for tile stores.

### 2.5 Validation `Raster::decode` performs (all formats)

A blob is rejected with a `CodecStatus` (never an exception) if:

- format is unknown;
- cellType ≠ Float64;
- bands, height or width ≠ the `TileSpec` passed in;
- noData ≠ `TileSpec.noData` (NaN compared as NaN);
- byte length ≠ preamble + 8·B·H·W.

Pitfall: `TileSpec.noData` defaults to 0.0. It must be NaN for every tile-store input, or every blob is rejected
(and 0.0 cells would be NoData).

---

## 3. Manifest (`manifest.json`) [IN FORCE; `record_version` 4 since 2026-10-21]

```json
{
  "format": "mabench-tilestore", "format_version": 1, "record_version": 4,
  "raster": {"width": W, "height": H, "bands": B, "transform": [a, b, c, d, e, f], "crs": "EPSG:…"},
  "tiling": {"tile_size": T, "tiles_x": ceil(W/T), "tiles_y": ceil(H/T), "tile_order": "row-major", "edge_padding": "nan"},
  "cells":  {"dtype": "float64", "byte_order": "little", "nodata": "nan",
             "layout": "band-major; row-major within band", "payload": "frame header (112 B, MPLFRM04) + NES MapletCodec format-3 maplets …"},
  "frames": [{"frame_id": 0, "event_time_ms": 0, "key": "frames/f000000.mpl", "n_records": N,
              "nbytes": …, "sha256": "…", "valid_cells": …, "source": {…}}],
  "provenance": {…}
}
```

Readers **fail fast** on any other `record_version`, byte order, dtype or NoData convention (decision D-P4: the
spec is the single source of truth, frozen per version). Output stores (reference, GeoTrellis, NES) keep the input's
raster and tiling, and record the workload and input frame checksums in `provenance`.

[PROPOSED] Optional `raster.band_names` (e.g. `["B04","B03","B02","B08"]`) for name-based band selection resolved at
planning time. Purely additive; it does not change any binary layout.

---

## 4. NES tuple mapping, current layout [IN FORCE for CSV systests; target of the RasterFile source without M1]

```
.mpl record                          NES tuple
tile header  event_time_ms  ──────►  ts       UINT64
             frame_id       ──────►  frameId  UINT32      (optional)
             tile_x         ──────►  tileX    INT32       (read by FOCAL_* for neighbour lookup / completeness bitmap)
             tile_y         ──────►  tileY    INT32
             bands, cols, rows, flags → checked against manifest, not emitted
maplet       all bytes      ──────►  payload  VARSIZED    (child buffer; copied 1:1, not decoded by the source)
```

```sql
SELECT tileX, tileY, FOCAL_SUM(payload, SQUARE, 1, NODATA, -9999, 4, 4, 2, 2) AS payload
FROM raster4x4 WINDOW TUMBLING(ts, size 100 ms) INTO FILE ();          -- existing CSV systest (text maplets)
```

Rules:

- **No `bands` column.** The band count lives in the maplet, and a column would go stale after `BANDS`.
- **One window = one frame.** The tumbling window size equals the event-time distance between frames (`mabench prepare --frame-period-ms`).

---

## 5. Self-locating maplet, record version 4 [IN FORCE: tooling, 2026-10-21; NES codec P2, source P5]

Supersedes draft 1 (24-byte frame header, position-only preamble). Principles agreed with Aaron:
- a `.mpl` file = **one header block + n maplet blocks**;
- raster-level facts live in the header; maplets are self-locating;
- redundancy is tolerated where it makes a maplet self-contained;
- NoData lives **only** at raster level (header, manifest), never in a maplet.

### 5.1 Frame header block (once per `.mpl` file), struct `<8sIIQIIIIBBHId II6d`, 112 B

```
+0    char[8] magic          = "MPLFRM04"
+8    u32     frame_id       file sequence number for mabench bookkeeping only; NES ignores it (see 5.4)
+12   u32     n_tiles        = tiles_x · tiles_y   (file framing: maplets that follow)
+16   u64     event_time_ms  frame-level event time: identical for all tiles of the frame
+24   u32     raster_width   W (true extent, before padding)
+28   u32     raster_height  H
+32   u32     tile_width     T_w
+36   u32     tile_height    T_h
+40   u8      bands          band count of the stored raster (source anchor)
+41   u8      cellType       1 = Float64
+42   u16     flags          = 0 (reserved)
+44   u32     reserved       = 0
+48   f64     noData         NaN in benchmark stores; the only NoData field in the file
+56   u32     crs_epsg       0 = unknown
+60   u32     reserved       = 0
+64   f64[6]  transform      affine (a, b, c, d, e, f), rasterio/GDAL order; x = a·col + b·row + c, y = d·col + e·row + f
+112  … n_tiles × MAPLET
```

tiles_x = ceil(W / T_w) and tiles_y = ceil(H / T_h) are derived, not stored.

### 5.2 Maplet block, binary wire format 3, struct `<BBBBIiiIIII`, 32 B preamble

```
+0   u8   format         = 3                      parse: version dispatch
+1   u8   cellType       = 1 (Float64)            parse: bytes per cell
+2   u8   bands          1..255                   shape (changes through BANDS)
+3   u8   flags          bit 0 = tile filled with NoData by an operator (not observed data)
+4   u32  frame_id       0 in files (= unassigned); written by the NES source / frame-emitting operators (see 5.4)
+8   i32  tile_x         0 ≤ tile_x < tiles_x     place
+12  i32  tile_y         0 ≤ tile_y < tiles_y     place
+16  u32  height         = T_h                    shape
+20  u32  width          = T_w                    shape
+24  u32  raster_width   = W                      place: edge truncation without context; validated against header
+28  u32  raster_height  = H
+32  f64[bands·height·width]   cells, band-major, row-major within a band; padding cells = NoData
```

- Maplet size `32 + 8·B·T_h·T_w`.
- Maplet *i* starts at `112 + i·(32 + 8·B·T_h·T_w)`.
- No NoData field: interpretation comes from the raster level (`TileSpec.noData` from the descriptor or a `NODATA` argument).

### 5.3 NES tuple

```
frame header event_time_ms ──► ts       UINT64     window clause / watermarks
source frame counter       ──► frameId  UINT32     user-facing (WHERE frameId = 17); = maplet frame_id
maplet (all bytes, frame_id patched by the source) ──► raster VARSIZED   the data; carries frame_id and position itself
```

```sql
CREATE LOGICAL SOURCE s2 (ts UINT64, frameId UINT32, raster VARSIZED);
SELECT FOCAL_SUM(BANDS(raster, 0), 'SQUARE', 1) AS raster FROM s2 WINDOW TUMBLING(ts, size 100 ms) INTO FILE (…);
```

### 5.4 Frame identity: assigned by NES, origin-scoped [DECIDED 2026-10-20]

Full rationale: `focal-operator-design.md`, decisions F5 and F6.

- **Who assigns it.** The NES source, not the producer. Each source instance counts the frames it emits, starting at **1**, strictly increasing in emission order. It writes the number into every maplet's `frame_id` (4 bytes at preamble offset +4, patched while copying into the child buffer) and into the scalar `frameId` field.
- **0 = unassigned.** The tiler writes 0 into every maplet. Any operator receiving `frame_id == 0` has found a programming error.
- **Identity is origin-scoped.** Operators key frames by `(originId of the tuple buffer, maplet.frame_id)`. Origin ids are unique per source instance, so different sources and `UNION`s cannot collide.
- **Renumbering at origin boundaries.** Every operator that emits frames under its own origin id (focal, later zonal) assigns its output frames new numbers from its own counter (from 1), and writes them into the output maplets during encoding. Maps, filters and `BANDS` keep the origin and do not touch `frame_id`.
- **Why it is also in the maplet.** Keying never depends on a scalar field that a projection might drop. The scalar `frameId` is a query convenience.
- **The header's `frame_id`** stays as the file sequence number for mabench (frame files, manifest). Cross-system matching (NES output vs. reference vs. GeoTrellis) uses **event time**, which is producer-given and unique per frame.
- **Restarts.** A restarted NES query starts its counters at 1 together with empty state, so nothing can collide. This mirrors NES sequence numbers.

### 5.4a Frame-atomic sources [DECIDED]

A frame is emitted entirely by one source instance. Parallelism is across frames, not within a frame. A `.mpl` source
(one frame file per frame) and a future GeoTIFF source (one file per frame) satisfy this naturally.

### 5.5 Source checks (all per frame / per tile, no cell decoding)

- **At `open()`:** the manifest and each frame header agree on W, H, T_w, T_h, bands, cellType, noData, CRS and transform. NoData consistency is guaranteed by construction, because the header is the only NoData field.
- **Per frame:** `event_time_ms` strictly increases per source (unique timestamps); the file holds exactly n_tiles maplets; the source assigns the next frame number (from 1).
- **Per maplet:** format = 3; cellType, bands, height, width, raster_width and raster_height equal the header; 0 ≤ tile_x < tiles_x and 0 ≤ tile_y < tiles_y; no position appears twice in the frame; byte length = 32 + 8·B·T_h·T_w; then the source patches `frame_id` and copies the bytes.

### 5.6 What changes where

| Codebase | Change |
|---|---|
| NES codec | format 3 encode/decode; `MapletHeader {format, cellType, bands, flags, frameId, tileX, tileY, height, width, rasterWidth, rasterHeight}`; `peekHeader`; `selectBands` copies the preamble unchanged except `bands`; formats 1/2 kept for the CSV systests until retired |
| NES focal | frame store keyed by `(originId, frame_id)`, per-frame state by position (`focal-operator-design.md`); output frames renumbered under the operator's origin; for formats 1/2 (CSV) the frame key falls back to `(originId, ts)` and the position to the `tileX`/`tileY` fields |
| NES source | `(ts, frameId, raster)`; checks of 5.5; frame counter from 1; patches maplet `frame_id` |
| mabench `format.py`, tiler, store, compare, verify | `RECORD_VERSION = 4`, frame header `<8sIIQIIIIBBHIdII6d>`, maplet preamble `<BBBBIiiIIII>`, no per-tile record header; tiler writes maplet `frame_id = 0`; position from the maplet, frame from the file / event time |
| GeoTrellis `Format.scala`, `FrameFile.scala`, `TileStore.scala` | same as mabench |
| manifest | `record_version: 4`; stays the store index (frame list, checksums, provenance) and repeats the raster block, which the source checks against every frame header |
| tests | new golden blobs (1-band, 2-band, edge tile) in `nes/tests/MapletCodecGoldenTest.cpp` and the Python sync test; `scripts/regen-fixtures.sh`; `crosscheck.cpp` |

## 6. Query functions (NES plugin) [IN FORCE: FOCAL_* with CSV; rest PROPOSED]

Registered as plugin functions; **no grammar change** (decision: avoid ANTLR changes). The arguments are ordinary
`functionCall` arguments.

### 6.1 FOCAL_*

```
FOCAL_SUM | FOCAL_MEAN | FOCAL_MINIMUM | FOCAL_MAXIMUM   (MIN/MAX accepted as aliases)
  ( raster_arg, shape, radius [, nodata [, <raster geometry, CSV fallback>]] )
```

| Position | Must be | Checked at |
|---|---|---|
| 0 | `raster_arg ::= field_ref(VARSIZED) | BANDS(raster_arg, IntConst, …)` | planning (structural allowlist) |
| 1 | string constant `'SQUARE'` \| `'CIRCLE'` (the current systest uses bare `SQUARE`) | planning |
| 2 | integer constant ≥ 1 (radius in cells) | planning |
| 3 | numeric constant: NoData sentinel (optional) | planning + runtime consistency with the maplet |
| 4+ | integer constants: raster/tile geometry (optional, all or none; only when no raster descriptor exists, e.g. CSV) | planning |

- The legacy positional form `FOCAL_SUM(payload, SQUARE, 1, NODATA, -9999, 4, 4, 2, 2)` (boundary keyword, NoData, then four geometry integers) stays supported until the CSV systests are migrated. **[OPEN]** confirm the order of the four geometry integers in the current implementation.
- **Semantics** (per band b, cell c of the full window raster A): `FOCAL_op(A)[b](c) = op{ A[b](c+δ) | δ ∈ N, c+δ ∈ extent(A), A[b](c+δ) ≠ NoData }`, or NoData if that set is empty. Edges are truncated; cells outside the raster count as absent. The neighbourhood is 2-D within a band; bands never mix. The output has the same bands, H and W as the input.
- **Neighbourhoods:** `SQUARE(r)` = (2r+1)²; `CIRCLE(r)` = cells with dx² + dy² ≤ r².
- **NoData resolution:**
  1. explicit argument;
  2. else the raster descriptor (manifest, NaN);
  3. else NaN.

  A mismatch with the maplet's `noData` is a planning error when known, otherwise a decode status.

### 6.2 BANDS [PROPOSED, next to implement]

```
BANDS(raster_arg, i₁, …, i_k)      k ≥ 1, distinct, 0-based, each < B of its input
```

- **Semantics:** `BANDS(T, i₁…i_k)[j] = T[i_j]`. The output has k bands in the listed order. Height, width, NoData (and the position under M1) are unchanged.
- **Nesting:** indices refer to the input of *that* call, so `BANDS(BANDS(p, 1, 2, 3), 0)` is source band 1.
- **Implementation:** no cell decoding.
  1. Validate the preamble.
  2. Allocate preamble + 8·k·H·W.
  3. Copy the preamble with `bands = k`.
  4. `memcpy` plane i_j from `preamble + i_j·H·W·8` to slot j.
- **Band count propagation:** the source descriptor gives B, `BANDS` gives k, and `FOCAL_*` expects k. The physical operator still checks each maplet's preamble.

---

### 6.3 Window semantics and state of FOCAL_* [DECIDED 2026-10-20]

D2: tumbling windows only; several frames per window; computation, emission and eviction per frame; the window is a
completion deadline. Full specification, state layout, emission algorithm and rationale: **`focal-operator-design.md`**.

## 7. RasterFile source (NES plugin) [PROPOSED]

- **Config:** `raster_file.store` (path to the store directory); optionally a frame range. Keys are declared in the plugin, because DescriptorConfig rejects unknown keys.
- **At open:** read `manifest.json`; reject an unsupported `record_version`/dtype/byte order/NoData; expose the raster descriptor (W, H, B, T, tiles_x, tiles_y, NoData, transform, CRS) as static config. A resolution rule hands it to `FOCAL_*` (completeness bitmap, edge truncation); until that rule exists, geometry comes from trailing `FOCAL_*` arguments.
- **Per frame:** read records in order.
- **Per tile:**
  1. Validate the tile header / maplet preamble against the manifest.
  2. Write the scalar fields (§4 or §5.3).
  3. Copy the maplet bytes unchanged into a child buffer.

  It does **not** decode cells.
- **Event time:** from the tile header (v3) or the frame header (v4).
- **Frame identity (v4):** the source counts frames from 1 and patches each maplet's `frame_id` (§5.4). Frames are emitted whole (§5.4a).

---

## 8. Output path back to a tile store [OPEN]

`mabench compare` needs NES output as a tile store. Either a `.mpl` sink plugin, or `mabench import` converting
whatever the NES `FILE` sink writes for a VARSIZED field (format to be inspected). The output must carry the frame
(window start or frame id) and, without M1, the tile position.

---

## 9. Where the format is implemented (keep in sync)

| Codebase | File | Role |
|---|---|---|
| mabench | `mabench/src/mabench/format.py` | Python source of truth: struct strings, `FrameHeader`, `MapletHeader`, `encode_maplet` / `decode_maplet`, `iter_maplets`, `check_maplet` |
| mabench | `mabench/src/mabench/golden.py` | generates `nes/tests/MapletGoldenV3.hpp` (golden frame header + 4 maplets) |
| GeoTrellis | `geotrellis-baseline/src/main/scala/…/tilestore/Format.scala`, `FrameFile.scala`, `TileStore.scala` | Scala reader/writer, header checks |
| NES | `nes-plugins/Functions/RasterFunctions/MapletCodec.{hpp,cpp}` (+ patch in `nes/`) | C++ codec |
| tests | `nes/tests/MapletGoldenV3.hpp` (format 3, read by `test_cpp_golden_v3_matches_encoder` and Scala `FormatSuite`; P2 codec tests use it); `nes/tests/MapletCodecGoldenTest.cpp` (format 2, `test_cpp_golden_v2_blobs_match_legacy_encoder`); `nes/tests/stores` (tiny v4 stores + oracle outputs, kept current by `test_nes_test_stores_match_what_mabench_writes`); `mabench/tests/test_fixtures.py` (Scala fixtures byte-identical); `nes/tests/crosscheck.cpp` (whole frame decode → encode identical) | sync guards |

---

## Change log

- **2026-10-21.** Record version 4 implemented in mabench and the GeoTrellis baseline (§5 IN FORCE: tooling). Details fixed during implementation: a frame header with `n_tiles` < tiles_x·tiles_y is legal framing (an output that lost tiles), readers report the missing tiles instead of rejecting the file; output frames written by GeoTrellis copy the input frame header byte for byte; a `null` transform in the manifest is written as the identity `(1, 0, 0, 0, 1, 0)`; `crs_epsg` comes from an `EPSG:n` CRS string, otherwise from rasterio, otherwise 0. Golden bytes moved to the generated `nes/tests/MapletGoldenV3.hpp`.

- **2026-10-20.** Frame identity moved to NES (§5.4): source-assigned per origin, from 1, 0 = unassigned in files, renumbered by frame-emitting operators, key `(originId, frame_id)`. Frame-atomic sources (§5.4a). Window/state semantics moved to `focal-operator-design.md`.
- **2026-10-20.** Record version 4 (§5) DECIDED, with frame_id in every maplet (sequential per store; uniqueness among frames in flight, enforced by the source and checked by FrameState against ts).
- **2026-10-20.** D2 window semantics recorded as DECIDED (§6.3): tumbling only, several frames per window, per-frame state, emission and eviction, window = deadline. §5 replaced by draft 2 of record version 4: a 112-byte frame header with all raster-level fields (incl. NoData, CRS, transform), and a 32-byte maplet preamble with frame_id, position, tile and raster dims, and no NoData. Tuple `(ts, frameId, raster)`.
- **2026-10-19.** Document created. v3 record / v2 maplet with multiband byte recorded as IN FORCE. Self-locating maplet (v4 frame / v3 maplet) recorded as PROPOSED. `BANDS`, `FOCAL_*` argument rules, the RasterFile source and the output path recorded.
