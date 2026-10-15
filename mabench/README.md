# mabench

Benchmark tooling for *Streaming Map Algebra: Windowed Raster Processing in NebulaStream*.
Entry point of the baseline pipeline: it cuts a raster into tiles once and writes a **tile store**
that NES and the GeoTrellis micro-batch job both read. It also computes the reference (oracle) result.

```
raster file(s) / generator ──► mabench prepare|synth ──► tile store ──┬─► NES query
                                                                      ├─► GeoTrellis micro-batch
                                                                      └─► mabench reference (oracle)
```

## Install

```bash
pip install -e ".[dev]"      # numpy, scipy, rasterio; pytest + fsspec for tests
pip install -e ".[blob]" s3fs   # only if a store should live in object storage
pytest                        # 49 tests
```

## Usage

`mabench <command>` and `python -m mabench <command>` are equivalent. `prepare` expands glob patterns itself (`data/raw/*.grib2`) and sorts the matches by file name, so patterns also work where no shell is involved, e.g. IDE run configurations. `--variable NAME` picks one variable from every container file (NetCDF, HDF5, HDF4, Zarr: anything GDAL exposes as subdatasets), e.g. `--variable CMI` for GOES. Plain rasters (GeoTIFF, COG, VRT, GRIB2) don't need it; select their bands with `--bands`.

**PyCharm.** `.run/` holds shared run configurations (synth, prepare MRMS, reference, compare). They appear under *Run → Edit Configurations* when the project is opened. Each runs module `mabench` with the project interpreter (the `.venv` with `pip install -e ".[dev]"`) and the project root as working directory. Use Debug instead of Run to stop at breakpoints in the tiler, oracle or comparator.

```bash
# real data: every file becomes one frame (GeoTIFF, COG, VRT, or NETCDF:"file.nc":CMI)
mabench prepare dem.tif --out stores/dem-256 --tile-size 256
mabench prepare goes/*.tif --out stores/goes-b13 --tile-size 256 --frame-period-ms 600000

# synthetic data, deterministic, any size
mabench synth --out stores/ramp --width 4096 --height 4096 --generator ramp --tile-size 256
mabench synth --out stores/nd --width 1000 --height 700 --generator normal \
              --param nodata_fraction=0.1 --param nodata_block=0,0,64,64 --frames 5 --tile-size 64

mabench inspect stores/dem-256
mabench verify  stores/dem-256            # exit code 1 on any problem

# workloads: one JSON read identically by oracle, NES and GeoTrellis
mabench workload focal:mean:square:3 -o workloads/mean-sq-3.json
mabench reference stores/dem-256 --workload workloads/mean-sq-3.json --out refs/dem-256/mean-sq-3

# correctness of a system's output (NES or GeoTrellis) against the oracle; exit code 1 on FAIL
mabench compare out/gt/dem-256/mean-sq-3 --reference refs/dem-256/mean-sq-3 --input stores/dem-256 \
                --json results/gt-mean-sq-3.json --heatmap results/gt-mean-sq-3.png
```

`compare` implements evaluation-spec §3.2:
- **Coverage.** Every tile is present exactly once.
- **NoData confusion matrix.**
- **Match rates and errors.** Exact-match and tolerance-match rates, max |error|, RMSE and max ULP distance.
- **Strata.** Everything is reported per stratum: interior, seam band, raster-edge band, near-NoData and near-missing-tile.

Pass criterion:
- **Min/Max** must be bit-identical.
- **Sum** must satisfy `|a−b| ≤ τ·Σ|v|` over the valid neighbourhood, with τ = 1e-12 by default (`--tau`).
- **Mean** uses the same bound divided by the neighbourhood size.
- In every case the NoData masks must agree and no tile may be missing.

The workload is read from the reference's provenance, and a mismatch with the candidate's provenance is an error. `--heatmap` needs matplotlib (`pip install -e ".[plot]"`).

Generators: `constant`, `ramp`, `checker`, `uniform`, `normal`, `waves` (smooth, terrain-like),
with NoData overlays `nodata_fraction` and `nodata_block`. Every value is a pure function of
`(seed, frame, band, y, x)`, so reads in any order and any window size agree.

## Tile store

A directory (or object-store prefix):

```
<store>/
  manifest.json            raster invariants, tiling, frames, checksums, provenance
  frames/f000000.mpl       one file per frame
  frames/f000001.mpl
```

One file per frame keeps writes atomic and resumable, lets a replayer stream frames in
event-time order, and keeps object sizes reasonable in blob storage. A single-stream file for a
source that wants one is simply the concatenation of the frame files in order, since each record
is self-describing.

### Frame file (record version 3)

| Part | Layout |
|---|---|
| Frame header, 16 B | `char[8] magic = "MPLFRM03"`, `u32 frame_id`, `u32 n_records` |
| Record header, 32 B | `u64 event_time_ms`, `u32 frame_id`, `u16 bands`, `u16 flags (0)`, `i32 tile_x`, `i32 tile_y`, `u32 cols`, `u32 rows` — the scalar fields of the NES tuple |
| Payload | exactly one NES `MapletCodec` `WireFormat::Binary` blob, passed to `Raster::decode` unchanged |

MapletCodec binary blob (from `MapletCodec.cpp`):

| Offset | Size | Field |
|---|---|---|
| 0 | 1 | format = 2 (`WireFormat::Binary`) |
| 1 | 1 | cell type = 1 (`CellType::Float64`) |
| 2 | 1 | bands (1..255), all bands of the tile |
| 3 | 1 | reserved, 0 |
| 4 | 4 | `u32` height (rows), **height before width** |
| 8 | 4 | `u32` width (cols) |
| 12 | 4 | reserved, 0 |
| 16 | 8 | `f64` noData sentinel, **NaN** in every benchmark store |
| 24 | 8·b·h·w | `f64` cells, band-major, row-major within a band, little-endian |

All integers are little-endian, and every offset is a multiple of 8, so readers can view cells in place.
Records are in row-major tile order (`tile_y`, then `tile_x`).

`Raster::decode` rejects a blob whose bands, height, width or noData differ from the stream's `TileSpec`. Two consequences:
- **Edge tiles must be padded.** Every tile has the full `tile_size`, and partial edge tiles are padded with NaN. This does not change any result under truncate-edge, NoData-ignoring semantics. `manifest.raster.width/height` give the true extent.
- **The NES `TileSpec` must set `format = Binary`, `bands = manifest.raster.bands` and `noData = NaN`.** The default `noData = 0.0` makes `decode` reject these blobs (`SizeMismatch`). It would also turn every genuine 0.0 into NoData.

All selected bands of a tile go into one maplet, band-major. Multiband needs the codec patch in `../nes/`; single-band blobs are unchanged by it.

**Cross-checked against the real codec.**
- `../nes/tests/MapletCodecGoldenTest.cpp` is a gtest with an embedded golden blob. It checks that `decode` reads it, that `encode` reproduces it byte for byte, and that a zero-noData spec is rejected. It has 1-band and 2-band golden blobs. It passes against the patched `MapletCodec.cpp` in `../nes/` and is meant to live next to the codec's unit tests in NES.
- A Python test keeps the embedded golden blob in sync with `encode_blob`.
- `../nes/tests/crosscheck.cpp` walks a whole frame file. It decodes each payload with `Raster::decode`, re-encodes it with `Raster::encode`, and requires identical bytes:

  `g++ -std=c++20 -I<RasterFunctions> MapletCodec.cpp nes/tests/crosscheck.cpp -o crosscheck && ./crosscheck store/frames/f000000.mpl`

`src/mabench/format.py` is the single source of truth for this layout on the Python side.

### Manifest (abridged)

```json
{
  "format": "mabench-tilestore", "format_version": 1, "record_version": 3,
  "raster": {"width": 8192, "height": 8192, "bands": 1, "transform": [a, b, c, d, e, f], "crs": "EPSG:4326"},
  "tiling": {"tile_size": 256, "tiles_x": 32, "tiles_y": 32, "tile_order": "row-major", "edge_padding": "nan"},
  "cells":  {"dtype": "float64", "byte_order": "little", "nodata": "nan", "layout": "band-major; row-major within band", "payload": "NES MapletCodec WireFormat::Binary blob (24-byte preamble, noData NaN)"},
  "frames": [{"frame_id": 0, "event_time_ms": 0, "key": "frames/f000000.mpl", "n_records": 1024,
              "nbytes": 536928272, "sha256": "…", "valid_cells": 67108864, "source": {"file": "dem.tif"}}],
  "provenance": {"source": {…}, "tool": "mabench", "version": "0.1.0", "argv": […], "created_utc": "…"}
}
```

## Design

| Module | Responsibility |
|---|---|
| `format.py` | byte layout; payload = NES `MapletCodec` binary blob (shared contract with NES and GeoTrellis `MapletIO`) |
| `manifest.py` | raster-level invariants, tiling, per-frame checksums and provenance |
| `storage.py` | `StorageBackend`: `LocalBackend` (atomic writes, mmap reads) and `FsspecBackend` (S3/GCS/Azure) |
| `sources.py` | `RasterSource`: `RasterioSource` (one file per frame), `SyntheticSource`, `ArraySource` |
| `tiler.py` | strip-wise tiling; memory ≈ `width × tile_size × bands × 8` bytes, independent of raster height |
| `store.py` | `TileStore`: read records, assemble frames, `verify()` |
| `oracle.py` | untiled reference for focal semantics, plus the naive per-cell definition it is tested against |
| `compare.py` | stratified correctness metrics, pass/fail verdict, JSON report, error heatmap |
| `workloads.py` | `Workload` base class; `FocalWorkload` (implemented), `ZonalWorkload` (interface only) |

**Workloads.** Each operator family is a `Workload` subclass that declares its parameters, its
`output_kind` (`RASTER` for focal, `TABLE` for zonal), its static `side_inputs()`, a
`prepare_side_inputs()` hook, `validate()` notes against a store, and `reference()`. Every later
pipeline stage dispatches on the workload, so adding zonal means filling in `ZonalWorkload`:
the zone polygons are declared as a side input already, and its hooks raise `NotImplementedError`
with a pointer to the open decisions in the evaluation spec.

**Object storage** is for staging datasets too large for the machine that prepares them. Timed
runs always read a local copy, so storage latency never enters a measurement.

## Measured on the dev container

8192 × 8192 float64 (512 MiB per frame), tile size 256: GeoTIFF → store in 2.0 s; `verify` in 0.6 s;
tiling memory stays near one strip of tiles (~180 MiB peak for the synthetic run).

## Known limits

- **Oracle speed.** Square neighbourhoods are summed separably (rows, then columns), so the cost per cell grows with r, not r². Measured on a 2048² frame with 10 % NoData: mean takes ~0.5–1 s and max ~0.2 s for r from 2 to 80. Circles use the direct 2-D path: ~1.6 s for r = 8 on the same frame.
- **Memory.** `reference` and `compare` work strip by strip (one tile row plus an `r`-row halo), so memory
  grows with raster width, not area. The results are bit-identical to whole-frame processing (tested). Measured
  on a 12000² frame (1.1 GiB): `reference` peaks at ~330 MiB, `compare --heatmap` at ~610 MiB. The heatmap is
  reduced to at most 2000 px per side (block maximum).
- Tiling is single-threaded. It is outside every timed region, so this only affects preparation time.
