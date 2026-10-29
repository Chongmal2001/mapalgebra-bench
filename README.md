# mapalgebra-bench

Benchmark and baseline tooling for the B.Sc. thesis *Streaming Map Algebra: Windowed Raster Processing in
NebulaStream* (TU Berlin, DIMA). It prepares raster data once as a binary **tile store**, runs focal map algebra
on it with a **GeoTrellis baseline**, computes a **reference** with an independent oracle, and **compares**
every system's output with the reference.

```
raster files / generator
        │  mabench prepare | synth
        ▼
   tile store ──┬──► NES query ─► Binary dump ─► import-nes  ──► output store ─┐
 (manifest +    ├──► geotrellis-baseline (BatchRunner)       ──► output store ─┼──► mabench compare ──► report, heatmap
  .mpl frames)  └──► mabench reference (oracle)              ──► reference ────┘
```

All three systems read and write the same tile store format, so nothing outside the map algebra differs
between them: no CSV parsing, no tiling inside the timed region, the same cells and the same NoData.

## Layout

| Path | What | Language |
|---|---|---|
| `mabench/` | tiler, tile store I/O, oracle, workloads, compare (CLI `mabench`) | Python 3.10+ |
| `geotrellis-baseline/` | GeoTrellis 3.8.1: full-grid correctness reference and micro-batch performance baseline | Scala 2.12, sbt |
| `scripts/` | `run-focal.sh` (one workload end to end), `run-perf.sh` (throughput runs), `sweep-correctness.sh` / `sweep-perf.sh` (the evaluation matrices), `fetch-s2.sh`, `gen-smoke-stores.sh` (synthetic smoke-test stores), `nes/build.sh`, `nes/run-query.sh` (NES in Docker, settings in `bench.env`) | bash |
| `docs/` | `maplet-format.md` (the NES contract: bytes, tuples, query functions), `architecture.md` (technical overview) | |
| `.run/` | shared PyCharm run configurations | |
| `data/` | stores, references, outputs, results; git-ignored | |

## Setup

```bash
# Python (from the repository root)
python -m venv .venv && source .venv/bin/activate
pip install -e "mabench[dev]"
(cd mabench && pytest)

# Scala (JDK 17 or 21, sbt 1.10+)
(cd geotrellis-baseline && sbt test)
```

IDEs: open the repository root in PyCharm (the run configurations in `.run/` use it as working directory) and
`geotrellis-baseline/` in IntelliJ IDEA (sbt import).

## New machine

Needs git, Docker (Linux: Docker Engine, user in the `docker` group; Docker Desktop: VM memory >= 16 GB), Python >= 3.10,
JDK 17 or 21 + sbt, and for `fetch-s2.sh` curl, jq and the GDAL command line tools. About 60 GB of free disk.

```bash
git clone <nes-fork-url> ~/nes && git -C ~/nes checkout <branch>
(cd ~/nes && scripts/install-local-docker-environment.sh)       # builds nebulastream/nes-development:local
git clone <mapalgebra-bench-url> ~/mapalgebra-bench && cd ~/mapalgebra-bench
echo 'NES_REPO=$HOME/nes' > bench.local.env                       # only if the NES clone is not ~/nes
scripts/nes/build.sh                                              # Release build in $NES_REPO/build-docker-release
python -m venv .venv && source .venv/bin/activate && pip install -e "mabench[dev]" && (cd mabench && pytest)
(cd geotrellis-baseline && sbt test)
scripts/fetch-s2.sh
```

## Typical run

```bash
scripts/nes/build.sh                 # once: NES worker + CLI in the NES development image
scripts/e2e-s2.sh                    # fetch S2 if missing, tile store, then per workload: GeoTrellis reference,
                                     # NES run (Docker), import, compare; summary table at the end
CLEAN=1 S2_FRAMES=3 scripts/e2e-s2.sh                     # start over, first 3 scenes only
WORKLOADS="focal:mean:circle:2 focal:sum:square:5" scripts/e2e-s2.sh
scripts/run-focal.sh data/stores/s2-berlin focal:mean:square:3   # a single workload
scripts/clean.sh [--raw]             # remove stores, references, runs, results (and the downloads)
```

Settings live in `bench.env`; machine-specific values (NES paths, memory) go into a git-ignored `bench.local.env`,
e.g. on a laptop with a CLion Docker build:

```bash
NES_REPO=$HOME/CLionProjects/nebulastream-BA-streaming-map-algebra
NES_BUILD=$NES_REPO/cmake-build-debug-docker
DOCKER_EXTRA_MOUNTS="$NES_REPO:/tmp/nebulastream-BA-streaming-map-algebra"
```

Results: `data/results/<store>/<workload>.{json,png}`; each NES run keeps its query (`topology.yaml`), logs and
provenance (`run.json`) in `data/runs/<store>/<workload>/`.

## Performance runs

```bash
scripts/run-perf.sh data/stores/s2-berlin focal:mean:square:3        # NES (Void sink) and GeoTrellis micro-batch,
                                                                      # 3 runs each, median table at the end
REPS=5 PERF_THREADS=4 SYSTEMS=nes scripts/run-perf.sh data/stores/s2-berlin focal:max:square:8
SYSTEMS=nes-passthrough scripts/run-perf.sh data/stores/s2-berlin focal:mean:square:3   # source + sink only
scripts/nes/profile.sh data/perf/<store>/<w>/nes-t1/rep1/topology.yaml data/profile/<w>    # perf profile of one run
```

Use a Release NES build (`NES_BUILD` without CLion's debug directory); the script warns otherwise. Each run keeps
its raw output in `data/perf/<store>/<workload>/<system>-t<threads>/rep<k>/`; `mabench perf-table data/perf --out
data/results/perf.csv` (run by the script) turns all of them into one CSV row per run for plotting.

## Evaluation matrices

```bash
scripts/sweep-correctness.sh data/stores/s2-berlin-16 data/stores/s2-berlin-16-t16   # every op and summation mode,
                                                                                     # radii, multi-frame windows, r > T
scripts/sweep-perf.sh data/stores/s2-berlin-16                                       # NES vs GeoTrellis, radii, windows
```

Both take their matrix from environment variables (header of each script) and skip finished cases.

## Sources of truth

- **Tile store format.** `mabench/src/mabench/format.py` (Python) and `geotrellis-baseline/.../tilestore/Format.scala`
  (Scala) implement the same frozen layout (record version 4, `docs/maplet-format.md` §5); both fail fast on anything else.
- **Kept in sync by tests.** The Scala `FormatSuite` reads two small mabench stores (commands in
  `geotrellis-baseline/src/test/resources/fixtures/README.md`), re-writes them and requires the same SHA-256.

See `docs/architecture.md` for how each component works.
