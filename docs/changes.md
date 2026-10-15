# Changes

## 2026-10-15: single repository

- **Merged into `mapalgebra-bench`.**
  - `mabench/`: Python.
  - `geotrellis-baseline/`: Scala.
  - `nes/`: was `mabench/nes-patch/` plus `mabench/tests/cpp/`.
  - PyCharm run configurations moved to `/.run`; data lives in `/data` (git-ignored).
- **Fixture sync test.**
  - `mabench/tests/test_fixtures.py` regenerates the GeoTrellis fixtures and requires byte-identical frames.
  - It immediately found that the committed reference fixtures predated the separable square summation in the oracle (last-bit differences, ≤ 3e-16 absolute). The fixtures were regenerated with `scripts/regen-fixtures.sh`.
- **Scripts.**
  - `scripts/run-focal.sh` runs GeoTrellis, the reference (once) and compare for one workload.
  - `scripts/regen-fixtures.sh` regenerates the fixtures.
- **Docs.** `docs/architecture.md` is the technical overview of all components.

## 2026-10-15: mabench handles large frames (reference, compare)

Problem: `compare` on a 3.5 GiB GOES C02 frame was killed by the Linux OOM killer with no Python error. It held the input, reference and candidate frames plus about a dozen full-frame helper arrays, over 40 GB. `reference` also assembled whole frames.

- **`store.py`.**
  - New `keys(frame)`: headers only, no cell copies.
  - New `assemble_rows(frame, row0, row1)`: decodes only tiles overlapping the rows.
  - `assemble` now calls `assemble_rows`.
- **`workloads.py`.** New `FocalReferenceSource`: the oracle computed per tile row on input rows with an r-row halo, written by the normal tiler. Bit-identical to whole-frame results.
- **`compare.py`.**
  - `compare_focal` works per tile row with an r-row input halo and adds up the metrics per stratum.
  - `strata_masks` gained `row0` / `height` and uses broadcast coordinates instead of full `mgrid` arrays.
  - New `_Heatmap` collects a block-maximum error map of at most 2000 px per side.
  - `_write_heatmap` takes the reduced arrays.
- **Tests.** +7: `assemble_rows`; reference bit-identical to the whole-frame oracle for four workloads including r > tile size; strip stratum masks equal to whole-frame masks; heatmap block maximum.
- **Measured** on a 12000² frame (1.1 GiB): `reference` peaks at 330 MiB, `compare --heatmap` at 610 MiB.

## 2026-10-14: geotrellis-baseline memory and usability

Problem: `OutOfMemoryError` on the first GOES frame. The heap was fixed at 4 GiB, and the runner held about 5 copies of a frame.

- **`FocalEngine`.**
  - `Prepared.headers` keeps only `RecordHeader`s, so input records become garbage after `prepare`.
  - Single-band tiles wrap the decoded array instead of copying it.
  - `toRecords` returns an `Iterator`.
- **Streaming writes.** `FrameFile.write` / `TileStoreWriter.writeFrame` gained variants taking a record count and an iterator, streaming one record at a time and checking the count. The `Seq` variants delegate to them.
- **`BatchRunner`.** Streams the output; new `checkHeap` (largest frame, heap, estimated 4× need; clear error with the `GT_HEAP` to use).
- **`build.sbt`.**
  - Heap from `GT_HEAP` (default `4g`), with `-Xms` = `-Xmx` and pre-touch.
  - `run / outputStrategy := Some(StdoutOutput)`, so progress on stderr is no longer shown as `[error]`.
- **`FocalWorkload`.** Passing a store manifest as `--workload` gives a clear message instead of a circe decoding error.
- **Result.** GOES-19 C02 (21696², 3.53 GiB, 7225 tiles), `focal:mean:square:3`, `GT_HEAP=17g`: 9.8 s timed compute, 25 s total.

## 2026-10-11: generic `--variable`

- **`cli.py`.** `--variable NAME` now works for every container format GDAL exposes as subdatasets (NetCDF, HDF5, HDF4, Zarr), matching on the variable name. A wrong name lists the variables that exist; it is an error on plain rasters.
- **`sources.py`.** A container opened without a variable fails with the list of its variables instead of "0 bands, no CRS".
