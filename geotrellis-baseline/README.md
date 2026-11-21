# geotrellis-baseline

GeoTrellis 3.8.1 baselines for *Streaming Map Algebra: Windowed Raster Processing in NebulaStream*. Reads the same
tile stores as NES (written by mabench) and writes results as tile stores, so `mabench compare` can line them up.

| Mode | Role | What it runs |
|---|---|---|
| `fullgrid` | correctness reference | each frame and band as one untiled `DoubleArrayTile`, focal operation on the whole grid: no tiles, no halos |
| `tiled` | compute core of the micro-batch baseline | each frame as a tiled layer, `CollectionFocalOperation` (halos from the 8 neighbouring tiles) |
| `microbatch` | performance baseline | frames replayed at a rate into event-time tumbling windows, each frame a tiled layer as in `tiled`; latency, throughput, CPU |

## Build, test, run

Requires JDK 17 or 21 and sbt 1.10+.

```bash
cd geotrellis-baseline
sbt test        # FormatSuite (tile store I/O vs mabench), FullGridSuite (full grid vs the focal definition), MicroBatchSuite

sbt "run --mode fullgrid --store ../data/stores/s2-berlin --workload focal:mean:square:3 --out ../data/refs/geotrellis/s2-berlin/mean-square-3"
mabench verify ../data/refs/geotrellis/s2-berlin/mean-square-3
```

## Micro-batch

```bash
sbt "run --mode microbatch --store ../data/stores/s2-berlin --workload focal:mean:square:3 --out ../data/runs/gt-mb/s2-berlin \
         --window-ms 432000000 --rate 0.5 --trigger frame --output checksum --warmup 2"
```

Mirrors a NES query `WINDOW TUMBLING(ts, size <window-ms> ms)` on the same store:

| Option | Meaning | Default |
|---|---|---|
| `--window-ms N` | event-time tumbling window size | smallest event-time step between frames (one frame per window) |
| `--rate FPS` | frames arrive every 1/FPS s, complete; `0` = all available at once (maximum throughput) | `0` |
| `--trigger frame` | compute each frame when it arrives (as NES emits a frame on completeness) | `frame` |
| `--trigger window` | compute all frames of a window when it closes (a frame of a later window arrives, or the stream ends) | |
| `--output checksum` / `store` / `none` | sum of output cells (inside the timed span) / tile store (to compare with the full-grid reference) / results discarded, like a NES Void sink (timed runs) | `checksum` |
| `--warmup N` | compute the first N frames once before timing | `0` |
| `--threads N` | worker threads for the kernel calls, all modes: microbatch / tiled run one kernel call per output tile and band on the pool (bands buffered in parallel), fullgrid computes stripes in parallel; results identical to 1 thread (fullgrid: see `--stripe-rows`) | `1` |
| `--stripe-rows N` | fullgrid with `--threads` > 1: stripe height; Min / Max bit-identical to the whole frame, Sum / Mean within rounding, independent of N threads; `0` = whole frame, parallel over bands only | `256` |

Frames never arrive incomplete, so there is no partial-frame path. Latency of a batch = end of its computation minus the
scheduled arrival of its last frame, so reading the frame from disk and waiting behind earlier batches count, as in NES
where the source reads inside the query. Output: `batches.csv` (one row per batch: window, frames, tiles, arrival, start,
done, latency, queueing, compute time), `summary.json` (parameters, elapsed time, compute / buffering / GC / CPU time,
throughput in cells/s, latency p50/p95/p99/max, checksum). Memory is not measured. Heap need: (frames held + 3) × the largest frame; with `--trigger window` all
frames of a window are held.

## Options

`--workload` takes a `workload.json` (`mabench workload`) or the short form. `--no-verify` skips the input checksum pass.
Output: a tile store with the input's raster, tiling and frame headers, provenance (`role`, `mode`, workload), and
`timings.csv` (compute time per frame, indicative only). `scripts/run-focal.sh` calls the full-grid mode as reference.

**Heap.** `run` forks a JVM with `-Xms = -Xmx = GT_HEAP` (default `4g`). The runner prints the largest frame and the
estimated need (4× the frame) and stops before reading if the heap is too small. One S2 frame (3801 × 3088, 4 bands,
≈ 390 MB) needs ≈ 1.6 GB, so the full S2 dataset runs in 4 GB; frames are processed one after another.

## Semantics

Kept aligned with NES and the evaluation spec:

- **NoData.** Tiles are `DoubleConstantNoDataCellType`, so NaN is NoData; NoData cells are skipped and an
  all-NoData window gives NaN. Missing input tiles are NoData in the assembled full grid.
- **Edges.** The window is truncated at the raster edge.
- **Neighbourhoods.** `Square(r)`; `Circle(r)` includes cells at distance ≤ r. `TargetCell.All`.
- **Multiband.** Bands are processed one after another.
- **Numerics.** Mean and Sum on squares use running sums: bit-identical on integer-valued data, last-bit differences
  on fractional data (covered by the τ tolerance in `mabench compare`). Min and Max are exact.
- **Tiled mode, r > tile size.** Halos come only from the 8 adjacent tiles, so cells further away are missing.

## Layout

| Path | Contents |
|---|---|
| `tilestore/` | `Format` (record version 4 byte layout), `FrameFile` (streaming frame reader/writer, SHA-256), `Manifest` (`manifest.json`, fails fast on other versions), `TileStore` (read, verify, write output stores) |
| `focal/FocalWorkload.scala` | workload → GeoTrellis neighbourhood and kernel |
| `focal/FullGrid.scala` | assemble frame → one tile per band → kernel → cut back into tiles |
| `focal/FocalEngine.scala` | tiled layer, `CollectionFocalOperation` (lazy result forced inside the timed region) |
| `runner/Main.scala` | command line, `--mode`, heap check, provenance and `timings.csv` |
| `runner/FullGridRunner.scala`, `runner/BatchRunner.scala`, `runner/MicroBatchRunner.scala` | the `fullgrid`, `tiled` and `microbatch` modes |
