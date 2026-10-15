# geotrellis-baseline

GeoTrellis 3.8.1 baseline for *Streaming Map Algebra: Windowed Raster Processing in NebulaStream*.
It reads the same tile stores as NES (written by `mabench`), runs a focal workload with GeoTrellis'
standard layer-level focal path, and writes the result as a tile store, so `mabench` can compare it
with the oracle and with NES.

```
mabench prepare/synth ──► tile store ──► BatchRunner (GeoTrellis) ──► output tile store + timings.csv
                                    └──► mabench reference (oracle) ──► reference tile store
```

## Build and test

Requires JDK 17 or 21 and sbt 1.10+.

```bash
sbt test      # FormatSuite + BaselineSuite against mabench fixtures in src/test/resources/fixtures
```

`BaselineSuite` is the first correctness comparison:
- **Six workloads** (mean, sum, min and max on square r = 2; mean and max on circle r = 3) must match the oracle on a 2-band, 2-frame store with 20 % NoData and a NoData block. Min and max must match exactly; sum and mean within 1e-9 relative.
- **One hypothesis test** checks the halo defect from `baseline-design.md` §2.1 at r = 20 with tile size 16. If it fails, GeoTrellis handles r > tile size correctly, and that finding must be dropped.

## Run

```bash
mabench synth --out stores/syn --width 2048 --height 2048 --generator normal --bands 2 \
              --param nodata_fraction=0.1 --frames 2 --tile-size 256
mabench workload focal:mean:square:3 -o workloads/mean-sq-3.json
mabench reference stores/syn --workload workloads/mean-sq-3.json --out refs/syn/mean-sq-3

sbt "run --store stores/syn --workload workloads/mean-sq-3.json --out out/gt/syn/mean-sq-3"
mabench verify out/gt/syn/mean-sq-3      # the GeoTrellis output is a valid tile store
```

`--workload` also accepts the short form `focal:mean:square:3`. `--no-verify` skips the input checksum pass.
`run` forks a JVM with a fixed heap, 4 GiB by default. Set `GT_HEAP` for large frames, e.g.
`GT_HEAP=20g sbt "run …"`: GeoTrellis holds a whole frame (one window) at once and needs about 4× its size.
The runner prints the largest frame, the heap and the estimate at startup, and stops before reading anything
if the heap is too small. Progress lines go to stderr; `build.sbt` forwards them without sbt's `[error]` prefix.

## Layout

| Path | Contents |
|---|---|
| `tilestore/Format.scala` | byte layout (record version 3), reading/writing headers and MapletCodec blobs; no dependencies |
| `tilestore/FrameFile.scala` | streaming frame reader/writer (no 2 GiB mmap limit), SHA-256, valid-cell count; no dependencies |
| `tilestore/Manifest.scala` | `manifest.json` via circe (transitive through GeoTrellis); fails fast on any other record version, byte order, dtype or nodata |
| `tilestore/TileStore.scala` | open, verify checksums, read frames, mask padding, write output stores |
| `focal/FocalWorkload.scala` | `workload.json` → GeoTrellis neighbourhood and kernel (`Mean`, `Sum`, `Min`, `Max`; `Square`, `Circle`) |
| `focal/FocalEngine.scala` | one window: `prepare` (untimed) → `compute` (timed) → `toRecords` (untimed) |
| `runner/BatchRunner.scala` | correctness baseline over all frames; writes output store + `timings.csv` |

## Semantics and fairness notes

- **Cell type.** Tiles are `DoubleArrayTile` with `DoubleConstantNoDataCellType`, so NaN is NoData, as in the store and in NES.
- **Neighbour assembly.** It goes through `CollectionFocalOperation` (`bufferTiles` + kernel on the buffered tile). It is inside the timed region, like halo resolution in NES.
- **Multiband.** GeoTrellis focal operations are single-band, so bands are processed one after another. This is also how its `MultibandTile` focal methods work.
- **Padding.** Output padding (cells outside the raster extent) is reset to NaN, so outputs pass `mabench verify` and compare cell for cell with the oracle.
- **Lazy results in Scala 2.12.** `CollectionFocalOperation` returns a lazily evaluated `Stream`: `BufferTiles` ends in `groupBy(...).mapValues(...).toSeq`. `FocalEngine.compute` forces it with `toVector`. Without that, the kernels would run after the timer stops and timings would be meaningless.
- **Timings.** `timings.csv` from `BatchRunner` is indicative only (no warm-up control). Performance numbers come from the micro-batch driver and JMH benchmarks (next steps).

## What has been verified, and how

- **Tile store core** (`Format`, `FrameFile`). Compiled with Scala 2.12.18 (no warnings under `-Xlint`) and run against the mabench fixtures:
  - blob bytes identical to the MapletCodec golden blob;
  - ramp values and padding cell-exact;
  - both frames of the `normal` store re-written byte-identically (same SHA-256 and valid-cell count as mabench);
  - truncated and wrong-version frames rejected.
- **GeoTrellis and circe code.** Every call was checked against the GeoTrellis 3.8.1 source. It compiles and runs with sbt 1.10.7 on JDK 21; a GOES-19 C02 frame (21696², 3.53 GiB, 7225 tiles) ran with `GT_HEAP=17g` in 9.8 s of timed compute (`focal:mean:square:3`).
- **Fixtures** are mabench output; `mabench/tests/test_fixtures.py` regenerates them and fails if any frame differs from the committed copy. Regenerate with `scripts/regen-fixtures.sh` after a deliberate format change.

## Memory

Per frame, the runner keeps: the input layer (single band wraps the decoded arrays, no copy), GeoTrellis'
buffered tiles, and the result layer. Input records are dropped after `prepare`, and output records are
produced lazily and streamed to disk one at a time (`FrameFile.write` with a record count and an iterator),
so peak heap is about 3-4× the frame. Frames run one after another, so the frame count does not matter,
only the largest frame.
