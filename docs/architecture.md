# Architecture

How the repository's components work, what each one guarantees, and how they behave on large inputs.
The benchmark definitions (semantics, metrics, research questions) live in the thesis evaluation spec; this
document covers the implementation.

## 1. Data flow

```
                         ┌──────────────────────────── tile store (input) ─────────────────────────────┐
 raw rasters ─ prepare ─►│ manifest.json   frames/f000000.mpl  frames/f000001.mpl  …                   │
 generator   ─ synth   ─►└───────┬───────────────────────┬───────────────────────────┬──────────────────┘
                                 │                       │                           │
                     NES focal operator      geotrellis-baseline BatchRunner   mabench reference (oracle)
                                 │                       │                           │
                           output store            output store               reference store
                                 └───────────────┬───────┴───────────────────────────┘
                                          mabench compare ──► per-stratum metrics, PASS/FAIL, JSON, heatmap
```

- Every arrow passes **files**, never in-process data. Each system's timing therefore covers only its own work.
- Inputs, outputs and the reference share one format and one tiling. That is what lets `compare` line them up
  tile by tile and cell by cell.
- A **workload** (`focal:<op>:<shape>:<radius>`, or the same as `workload.json`) is the second input to every
  system. Output stores record it in their provenance, and `compare` refuses outputs of different workloads.

## 2. Tile store format (record version 3)

```
<store>/manifest.json         raster (width, height, bands, transform, crs), tiling (tile_size, tiles_x, tiles_y),
                              cell layout, one entry per frame (key, n_records, nbytes, sha256, valid_cells), provenance
<store>/frames/fNNNNNN.mpl    one file per frame
```

| Part | Bytes | Layout (little-endian) |
|---|---|---|
| frame header | 16 | `char[8] "MPLFRM03"`, `u32 frame_id`, `u32 n_records` |
| record header | 32 | `u64 event_time_ms`, `u32 frame_id`, `u16 bands`, `u16 flags`, `i32 tile_x`, `i32 tile_y`, `u32 cols`, `u32 rows` |
| payload | 24 + 8·b·rows·cols | NES `MapletCodec` `WireFormat::Binary` blob: `u8 format=2`, `u8 cellType=1 (f64)`, `u8 bands`, pad, `u32 height`, `u32 width`, pad, `f64 noData=NaN`, then cells band-major, row-major within a band |

Invariants:

- **Self-describing records.** Every record states its own size, so a reader walks the file record by record.
- **Fixed record size.** All tiles have the full `tile_size`, and edge tiles are padded with NaN, which `Raster::decode` requires. So record *i* also starts at the fixed offset `16 + i·R`.
- **Fixed order.** Records are in row-major tile order (`tile_y`, then `tile_x`).
- **NaN is NoData** everywhere, and the blob's `noData` field must be NaN.
- **Frame files concatenate.** A file with several frames is just frame files concatenated in order.

## 3. mabench (Python)

### 3.1 Modules

| Module | Responsibility | Key behaviour |
|---|---|---|
| `format.py` | byte layout | `RecordHeader` (pack/unpack), `encode_blob` / `decode_blob` (validates format, cell type, bands, height, width and NaN noData exactly as `Raster::decode`), `iter_records` (yields header plus a zero-copy view of the cells; rejects trailing bytes) |
| `manifest.py` | `manifest.json` | dataclasses `RasterInfo`, `TilingInfo`, `FrameInfo`, `Manifest`; `format_version 1`, `record_version 3` |
| `storage.py` | where bytes live | `StorageBackend`: `LocalBackend` (atomic writes via temp file + `os.replace`, mmap reads) and `FsspecBackend` (s3/gs/az/memory, for staging) |
| `sources.py` | where frames come from | `RasterSource` → frames → `FrameReader.read(row, col, rows, cols)` returning float64 `(bands, rows, cols)` with NaN. `RasterioSource`: one file per frame; NoData masks → NaN; scale/offset applied; all files must share one grid. `SyntheticSource`: every value is a pure function of (seed, frame, band, y, x) (counter-based splitmix64), so any read order gives the same values. `ArraySource`: in-memory frames |
| `tiler.py` | source → store | per frame: reads one strip (one tile row, full width), cuts it into NaN-padded tiles, streams header + blob to the frame file while computing SHA-256 and the valid-cell count; writes the manifest at the end |
| `store.py` | reading stores | `records` (copies), `keys` (headers only), `assemble_rows(frame, row0, row1)` (decodes only tiles overlapping those rows), `assemble` (= all rows), `verify()` (checksums, header consistency, grid completeness, duplicates, NaN padding, valid-cell count) |
| `oracle.py` | reference semantics | `focal(a, op, fp)` on an untiled 2-D array: sum/mean via zero-filled window sums plus an exact window count; min/max via ±inf-filled filters. Square footprints are summed separably (rows, then columns); circles use 2-D correlation. `naive_focal` is the literal per-cell definition used to test `focal` |
| `workloads.py` | operator families | `Workload` base class with a registry; `FocalWorkload` (validate, reference, compare); `ZonalWorkload` (interface only: zones as a side input, table output) |
| `compare.py` | correctness metrics | coverage, NoData confusion, exact and tolerance match, max abs error, RMSE, max ULP, per stratum; PASS/FAIL; JSON report; heatmap |
| `cli.py` | commands | `prepare`, `synth`, `inspect`, `verify`, `workload`, `reference`, `compare` |

### 3.2 `prepare`

1. **Expand the inputs.** Glob patterns are expanded by mabench itself (so IDE run configurations work) and sorted by file name. File-name order is time order for GOES and MRMS.
2. **Pick a variable from containers.** `--variable NAME` selects one variable from every container file: NetCDF, HDF5, HDF4, Zarr, i.e. anything GDAL exposes as subdatasets. The match is on the name after the last `:` or `/`. A wrong name lists the variables that exist; using it on a plain raster is an error.
3. **Read with `RasterioSource`.** The first file fixes the grid (size, transform, CRS) and the bands (`--bands`, default all). Every later file must match it.
4. **Tile.** The tiler writes one frame per file. Event times are `i × --frame-period-ms`.

### 3.3 `reference`

`FocalReferenceSource` is a `RasterSource` whose frames are the oracle's output, and the ordinary tiler writes it as a store:

1. The tiler asks for output rows [y0, y1) (one tile row).
2. The reader loads input rows [y0 − r, y1 + r), clipped to the raster, with `assemble_rows`. These rows contain every neighbourhood of the requested rows.
3. It runs the oracle per band and returns rows [y0, y1).

Each output cell is computed from the same inputs in the same order as on the whole frame, so the result is **bit-identical** to the whole-frame oracle. It's tested for r up to 20 on 16-px tiles, where neighbourhoods span several tile rows. Memory is about `width × (tile_size + 2r) × bands × 8` bytes times a few intermediate arrays.

### 3.4 `compare`

Per frame, then per tile row with an r-row input halo:

1. **Coverage.** Frames missing on either side, tiles missing from the candidate, duplicate tiles.
2. **Read three strips.** Input rows with halo, reference rows, candidate rows (`assemble_rows`).
3. **Tolerance.** Sum and mean only: `τ · window-sum(|v|)` (sum) or `τ · window-mean(|v|)` (mean), computed with the oracle on the input strip. τ = 1e-12 by default.
4. **Stratum masks.** Computed for the strip with global coordinates (`row0`, total `height`), using broadcast 1-D coordinates instead of full grids:
   - `interior`
   - `seam` (neighbourhood crosses an internal tile edge)
   - `raster_edge` (neighbourhood truncated)
   - `near_nodata`
   - `near_missing_tile`
5. **Metrics.** Per stratum, added up across strips: cell counts, NoData confusion matrix, exact matches, within-tolerance matches, max |error|, sum of squared errors, max ULP distance.
6. **Heatmap** (frame 0 only). Errors are reduced to at most 2000 px per side by **block maximum**, so no error disappears in the downsampling. NoData mismatches are drawn in red; tile lines are drawn while they remain readable.

PASS requires all of the following:

- every tile present exactly once;
- the NoData masks agree;
- bitwise equality for min/max, or for sum/mean every cell within its tolerance.

### 3.5 Memory and time (measured)

| Command | Memory | Measured |
|---|---|---|
| `prepare` / `synth` | one strip of tiles | 12000² synthetic: ~270 MiB peak |
| `reference` | one strip plus halo | 12000² (1.1 GiB frame): 330 MiB, 17 s |
| `compare --heatmap` | three strips plus halo | 12000²: 610 MiB, 52 s |

Memory grows with raster **width**, not area or frame count. Frames are processed one after another.

## 4. geotrellis-baseline (Scala)

Package `de.tuberlin.dima.geotrellisbaseline`; GeoTrellis 3.8.1 (`geotrellis-raster`, `geotrellis-layer`), Scala 2.12.

### 4.1 Modules

| File | Responsibility |
|---|---|
| `tilestore/Format.scala` | the byte layout: frame header, record header, `readBlob` / `writeBlob` (same validation as `Raster::decode`), `Record(header, cells)` with `band(b)`. No dependencies |
| `tilestore/FrameFile.scala` | streaming frame reader (`FileChannel`, record by record; no 2 GiB mmap limit) and writer (atomic temp file + rename, SHA-256 and valid-cell count while writing). `write(path, id, n, iterator, …)` streams records and checks that exactly `n` were written |
| `tilestore/Manifest.scala` | `manifest.json` via circe; fails fast on any other record version, byte order, dtype or NoData convention |
| `tilestore/TileStore.scala` | open a store, `verifyChecksums`, `readFrame` (checks headers against the manifest), `maskPadding`; `TileStoreWriter` writes an output store with the input's raster and tiling |
| `focal/FocalWorkload.scala` | workload JSON or short form → GeoTrellis `Square(r)` / `Circle(r)` plus kernel `Mean` / `Sum` / `Min` / `Max` with `TargetCell.All` |
| `focal/FocalEngine.scala` | one window = one frame: `prepare` → `compute` → `toRecords` |
| `runner/BatchRunner.scala` | CLI: `--store --workload --out [--no-verify]`; heap check; runs all frames; writes output store and `timings.csv` |

### 4.2 One frame through `BatchRunner`

```
readFrame          .mpl → Vector[Record]                       untimed  (FrameFile.read + header checks)
prepare            per band: Seq[(SpatialKey, DoubleArrayTile)] untimed  (1 band: wraps the decoded array, no copy;
                                                                          keeps only headers, so input records can be freed)
compute            per band: CollectionFocalOperation(layer, neighborhood, layerBounds)(kernel).toVector
                                                                TIMED    (bufferTiles: 8 neighbour slices per tile, stitched
                                                                          into (T+2r)² tiles; kernel on the inner T×T)
toRecords + write  Iterator[Record] → FrameFile.write          untimed  (one output record exists at a time; padding reset to NaN)
```

- **Forcing the result.** `toVector` is required: in Scala 2.12, `CollectionFocalOperation` returns a lazy `Stream`, and without forcing it the kernels would run after the timer stops.
- **Bands.** They are processed one after another, as GeoTrellis' own `MultibandTile` focal methods do.
- **Cell type.** Tiles use `DoubleConstantNoDataCellType` (the `DoubleArrayTile.apply` default), so NaN is NoData.

### 4.3 Memory and heap

- **Peak heap per frame.** About input layer (F) + GeoTrellis' slices and buffered tiles (≥ F) + result layer (F), estimated as 4F.
- **Heap check.** `checkHeap` prints the largest frame, the heap and that estimate, and stops before reading anything if `4F > heap`.
- **Heap setting.** `GT_HEAP` (default `4g`) sets `-Xms` = `-Xmx` with `-XX:+AlwaysPreTouch`, so the heap never grows during a timed region.
- **Compressed pointers.** Keep heaps ≤ 31g, or go well above 32g; just above 32 GB the JVM loses compressed object pointers.
- **Frame count.** Frames run one after another, so peak memory depends only on the largest frame.

This 4F footprint is inherent to the micro-batch design: the whole window is in memory. It is a result for RQ2,
not something to optimise away. A streaming focal operator needs about two tile rows plus halos.

### 4.4 Output and logging

- **Output store.** Same raster, tiling and frame ids as the input, with provenance (`role: geotrellis`, GeoTrellis version, workload, input checksums).
- **`timings.csv`.** One line per frame: `frame_id, tiles, bands, cells, compute_ns`. Indicative only; there is no warm-up control yet.
- **Logging.** Progress goes to stderr. `build.sbt` sets `run / outputStrategy := Some(StdoutOutput)`, so sbt doesn't prefix it with `[error]`.

### 4.5 GeoTrellis semantics that matter for the comparison

- **Edges.** Truncated at the raster edge. A missing neighbour tile contributes nothing, i.e. it behaves like an edge.
- **NoData.** NaN cells are skipped; an all-NoData window gives NaN.
- **Circle.** `Circle(r)` includes cells with distance ≤ r, matching the spec.
- **Kernel strategies.**
  - Mean and Sum on squares use running sums (add the new column, subtract the old one), so rounding drift along long rows is possible.
  - Circles use a cursor that tracks cells entering and leaving the mask.
  - Min and Max visit every cell of the window for each output cell.
- **Halo limit.** Halos come only from the **8 direct neighbours**, cropped to the neighbour's size. For r > tile size, cells further away are silently missing from the window; `BaselineSuite` tests this at r = 20 on 16-px tiles.

## 5. nes/

- **`MapletCodec.cpp.patch` / `MapletCodec.cpp`.** Multiband support: the band count goes in preamble byte 2, and single-band blobs are byte-identical to before. The required header changes are listed in `nes/README.md`.
- **`tests/MapletCodecGoldenTest.cpp`.** A gtest with 1- and 2-band golden blobs: decode, byte-identical encode, a zero-noData spec rejected. The same bytes are checked against mabench's encoder in `mabench/tests/test_pipeline.py`.
- **`tests/crosscheck.cpp`.** Decodes and re-encodes every record of a real frame file with the NES codec and requires identical bytes.

## 6. Verification matrix

| What | How |
|---|---|
| mabench blobs = NES codec | golden test (C++), golden sync test (Python), `crosscheck` on whole frames |
| GeoTrellis tile I/O = mabench | `FormatSuite`: re-written frames have the same SHA-256 as mabench's |
| GeoTrellis fixtures = current mabench | `test_fixtures.py` regenerates every fixture and compares frame hashes |
| oracle = spec | `naive_focal` comparison, all-NoData windows, tile-size invariance |
| strip processing = whole-frame processing | reference bit-identical; stratum masks identical; `assemble_rows` = slices of `assemble` |
| GeoTrellis = oracle (small) | `BaselineSuite`: six workloads; halo-defect hypothesis at r > tile size |
| compare logic | identical, within-tolerance, ULP, missing tiles, NoData mismatches, stratum attribution, one-tile-halo model |

## 7. Known limits and next steps

- **No frame selection or resume.** Output manifests are written after the last frame, so an interrupted run has to start over.
- **`compare` is single-threaded** and parses every record header for each strip. It could jump to fixed offsets and compare frames in parallel.
- **`BatchRunner` has no performance control.** For performance numbers it will be replaced by the micro-batch driver (arrival schedule, `Complete` / `Interval(Δ)` triggers, per-window latency) and JMH benchmarks.
- **Planned runner modes.** `--mode raster` (whole-frame GeoTrellis, as a cross-check that separates kernel errors from tiling errors) and `--mode strip` (GeoTrellis made to stream, as the strongest possible baseline).
- **Zonal workloads.** Interface only.
