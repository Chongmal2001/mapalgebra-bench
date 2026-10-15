# mapalgebra-bench

Benchmark and baseline tooling for the B.Sc. thesis *Streaming Map Algebra: Windowed Raster Processing in
NebulaStream* (TU Berlin, DIMA). It prepares raster data once as a binary **tile store**, runs focal map algebra
on it with a **GeoTrellis baseline**, computes a **reference** with an independent oracle, and **compares**
every system's output with the reference.

```
raster files / generator
        │  mabench prepare | synth
        ▼
   tile store ──┬──► NES query (focal operator)              ──► output store ─┐
 (manifest +    ├──► geotrellis-baseline (BatchRunner)       ──► output store ─┼──► mabench compare ──► report, heatmap
  .mpl frames)  └──► mabench reference (oracle)              ──► reference ────┘
```

All three systems read and write the same tile store format, so nothing outside the map algebra differs
between them: no CSV parsing, no tiling inside the timed region, the same cells and the same NoData.

## Layout

| Path | What | Language |
|---|---|---|
| `mabench/` | tiler, tile store I/O, oracle, workloads, compare (CLI `mabench`) | Python 3.10+ |
| `geotrellis-baseline/` | GeoTrellis 3.8.1 baseline: reads tile stores, runs focal workloads, writes output stores | Scala 2.12, sbt |
| `nes/` | multiband patch for NES `MapletCodec`, C++ golden-blob test and frame cross-check | C++20 |
| `scripts/` | `run-focal.sh` (one workload end to end), `regen-fixtures.sh` | bash |
| `docs/` | `architecture.md` (technical overview), `changes.md` (change log) | |
| `.run/` | shared PyCharm run configurations | |
| `data/` | stores, references, outputs, results; git-ignored | |

## Setup

```bash
# Python (from the repository root)
python -m venv .venv && source .venv/bin/activate
pip install -e "mabench[dev]"
(cd mabench && pytest)                      # 49 tests

# Scala (JDK 17 or 21, sbt 1.10+)
(cd geotrellis-baseline && sbt test)
```

IDEs: open the repository root in PyCharm (the run configurations in `.run/` use it as working directory) and
`geotrellis-baseline/` in IntelliJ IDEA (sbt import).

## Typical run

```bash
mabench prepare data/raw/goes/*.nc --variable CMI --out data/stores/goes19 --tile-size 256 --frame-period-ms 600000
mabench verify data/stores/goes19
GT_HEAP=20g scripts/run-focal.sh data/stores/goes19 focal:mean:square:3
# -> data/out/gt/goes19/mean-square-3, data/refs/goes19/mean-square-3, data/results/goes19/mean-square-3.{json,png}
```

## Sources of truth

- **Tile store format.** `mabench/src/mabench/format.py` (Python) and `geotrellis-baseline/.../tilestore/Format.scala`
  (Scala) implement the same frozen layout (record version 3); both fail fast on anything else.
- **Kept in sync by tests.** `mabench/tests/test_pipeline.py` checks the encoder against the golden blobs in
  `nes/tests/MapletCodecGoldenTest.cpp`. `mabench/tests/test_fixtures.py` regenerates the GeoTrellis test fixtures
  and requires them to be byte-identical to the committed copies.

See `docs/architecture.md` for how each component works.
