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
| `scripts/` | `correctness.sh` / `performance.sh` (the evaluation end to end, cases / grids from plain-text definitions), `smoke.sh` (pipeline check on tiny synthetic stores), `report.sh`, `clean.sh`; `data/` (fetch, stores), `correctness/` (`case.sh` one workload, `suite.sh` the matrix), `perf/` (`run.sh` one workload, `grid.sh` the matrix), `nes/` (build, bundle, worker start / stop, one query), `lib/` (settings from `bench.env`, provenance) | bash |
| `docs/` | `maplet-format.md` (the NES contract: bytes, tuples, query functions), `architecture.md` (technical overview) | |
| `.run/` | shared PyCharm run configurations | |
| `data/` | raw scenes, stores, references (inputs and caches); `results/<label>/` result sets; git-ignored | |

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
JDK 17 or 21 + sbt, and for `scripts/data/fetch-s2.sh` curl, jq and the GDAL command line tools. About 60 GB of free disk.

```bash
git clone <nes-fork-url> ~/nes && git -C ~/nes checkout <branch>
(cd ~/nes && scripts/install-local-docker-environment.sh)       # builds nebulastream/nes-development:local
git clone <mapalgebra-bench-url> ~/mapalgebra-bench && cd ~/mapalgebra-bench
echo 'NES_REPO=$HOME/nes' > bench.local.env                       # only if the NES clone is not ~/nes
scripts/nes/build.sh                                              # Release build in $NES_REPO/build-docker-release
python -m venv .venv && source .venv/bin/activate && pip install -e "mabench[dev]" && (cd mabench && pytest)
(cd geotrellis-baseline && sbt test)
scripts/data/stores.sh                                            # fetch S2, build the stores
```

## Typical run

```bash
scripts/smoke.sh                     # minutes: tiny synthetic stores, NES + GeoTrellis + compare + perf + report
scripts/correctness.sh               # correctness cases CASES (bench.env), stores on demand, report
scripts/performance.sh               # performance grids GRIDS (bench.env), stores on demand, report
RUN_LABEL=vm-final scripts/performance.sh G1 G4                           # some grids into data/results/vm-final
RUN_LABEL=vm-final scripts/performance.sh G2                              # later: one more grid, same result set
RUN_LABEL=try FILTER="op=mean radius=1,4" scripts/correctness.sh K1       # part of a case
scripts/correctness/case.sh data/stores/s2-berlin-16 focal:mean:square:3  # a single workload
scripts/data/stores.sh --list        # store names and recipes; scripts/data/stores.sh NAME builds one
scripts/report.sh [data/results/<label>]                                 # figures and tables again
scripts/clean.sh [--results LABEL|all] [--raw]   # remove stores and references (and result sets, downloads)
```

The evaluation plan lives in three plain-text files, so a subset can run now and the rest later with the same
configuration (finished runs of a result set are skipped):

| File | What |
|---|---|
| `scripts/correctness/cases.txt` | correctness cases K1–K12: `ID STORES OPS:SHAPES:RADII [KEY=V1,V2 ...]`, comma lists expand to all combinations; `SUMMATION=all`, `WINDOW_FRAMES`, `WINDOW_SLIDE`, `NES_INPUT`, `NES_THREADS`, `EXPECT=fail/error` |
| `scripts/perf/grids.txt` | performance grids G1–G7, same syntax; `WINDOW_FRAMES`, `PERF_THREADS`, `SYSTEMS`, `VARIANT` + `GT_JAVA_OPTS`, `RATE`, `NES_EVENT_TRACE` |
| `scripts/data/stores.txt` | stores by name: `s2 FRAMES TILE`, `derive BASE ARGS` (mabench derive), `synth ARGS` |

`CASES` / `GRIDS` in `bench.env` (or arguments) select IDs, `FILTER="op=max radius=4"` narrows them, a line with
`REQUIRES=<feature>` runs only when the feature is in `FEATURES` (e.g. `nes-pacing`: paced NES runs, which need a NES build with the paced RasterFile source and the FrameLatency sink; G6 and K12).

Paced runs (`RATE` frames/s): GeoTrellis releases frame i at t0 + i / rate; NES gets `FRAME_INTERVAL_US` = 1e6 / RATE on the RasterFile source and the FrameLatency sink instead of Void. The source writes `pacing.csv` (scheduled and actual release per frame), the sink `frame-latency.csv` (first and last output per source frame), both on the worker's steady clock; `mabench perf-table` turns them into window latency (as GeoTrellis reports it), frame latency, time to first output and the source's release lag.
Data: `S2_*` in `bench.env` (tile, dates, crop window, frames per store); scenes that are NoData everywhere are skipped.

Settings live in `bench.env`; machine-specific values (NES paths, memory) go into a git-ignored `bench.local.env`,
e.g. on a laptop with a CLion Docker build:

```bash
NES_REPO=$HOME/CLionProjects/nebulastream-BA-streaming-map-algebra
NES_BUILD=$NES_REPO/cmake-build-debug-docker
DOCKER_EXTRA_MOUNTS="$NES_REPO:/tmp/nebulastream-BA-streaming-map-algebra"
```

Results: one directory per result set, `RESULTS` = `data/results/<RUN_LABEL>` (default label: host name; set
`RUN_LABEL` or `RESULTS` per run or in `bench.local.env`). Same label = same set, so a restart resumes it.

```
data/results/<label>/
  provenance/<utc>-<script>.json   command, start / end, exit status, host, OS, CPU model + ISA, memory, bench and NES
                                   commits + dirty flags (native: bundle BUILD_INFO), Java, Python / NumPy / mabench,
                                   input stores with manifest SHA-256
  provenance/<utc>-<script>.env    every effective setting (NES_*, GT_*, S2_*, matrix variables, ...)
  provenance/<utc>-<script>.diff   uncommitted bench changes, if any
  correctness/<case>/<store>/<run>.json|png   compare reports and error heatmaps; outcomes.csv (expected vs actual)
  runs/<case>/<store>/<run>/       NES correctness runs: topology.yaml, logs, status.json, run.json
  perf/<store>/<w>/<system>-t<threads>/rep<k>/   perf runs: meta.json, run.json / gt logs
  perf.csv, report/                tables and figures (scripts/report.sh)
```

## Performance runs

```bash
scripts/perf/run.sh data/stores/s2-berlin-16 focal:mean:square:3        # NES (Void sink) and GeoTrellis micro-batch,
                                                                      # 3 runs each, median table at the end
REPS=5 PERF_THREADS=4 SYSTEMS=nes scripts/perf/run.sh data/stores/s2-berlin-16 focal:max:square:8
SYSTEMS=nes-passthrough scripts/perf/run.sh data/stores/s2-berlin-16 focal:mean:square:3   # source + sink only
scripts/nes/profile.sh data/results/<label>/perf/<store>/<w>/nes-t1/rep1/topology.yaml data/profile/<w>    # perf profile of one run
```

Use a Release NES build (`NES_BUILD` without CLion's debug directory); the script warns otherwise. Each run keeps
its raw output in `$RESULTS/perf/<store>/<workload>/<system>-t<threads>/rep<k>/`; `mabench perf-table` (run by
the script, output `$RESULTS/perf.csv`) turns all of them into one CSV row per run for plotting.

One measured run: the store is read into the page cache (`WARM_INPUT=1`, `scripts/lib/warm.sh`); NES: a worker is
started (`scripts/nes/worker.sh start`, resource sampler `scripts/lib/sample.sh`), the same query runs untimed on the
store's first frame (`NES_WARMUP=1`, store `<store>-warm1` made once by `mabench derive`), then the timed query, then
the worker is stopped; GeoTrellis computes `GT_WARMUP` untimed frames in its own JVM. Memory is measured for NES only
(bounded state is a requirement on the operator); GeoTrellis records no memory.
`scripts/nes/run-query.sh` stays one query: with `NES_WORKER_DIR` it uses a running worker, otherwise it starts and
stops one for that query.

| Metric (`perf.csv`) | NES | GeoTrellis |
|---|---|---|
| `elapsed_s` | query Running → Stopped (worker timestamps) | first frame scheduled → last batch done |
| `compute_s`, `buffer_s`, `gc_s` | – | focal operation, its neighbour-buffering part, GC inside the span |
| `cpu_s`, `cells_per_cpu_s` | worker user + system CPU inside the span (samples, interpolated) | JVM process CPU inside the span |
| `peak_rss_bytes`, `mean_rss_bytes` | sampled worker RSS inside the span | – |
| `rss_hwm_bytes` | VmHWM over the worker's life (incl. start-up, warm-up) | – |
| latency p50/95/99 | paced runs: per window, per frame, first output | per batch |

The NES worker's resource samples (`resources.csv`, every `NES_SAMPLE_MS`, default 100 ms) also feed the RSS-over-time
figure of `mabench report`. Figures are written as PNG. Older `perf.csv` files (`wall_s`, `peak_mem_bytes`) are still
read.

## Derived stores

`mabench derive STORE --out OUT` makes a store from another one, same raster and tiling, frames renumbered 0..n-1:
`--frames 0:4` (subset), `--bands 3,1` (select / reorder), `--scale 1e-4 --offset -0.1` (e.g. S2 reflectance from DN),
`--nan F:fraction=P | F:block=X0,Y0,W,H | F:tile=X,Y` (injected NoData), `--drop-tiles F:X,Y | F:row=Y | F:col=X`
(tiles left out of a frame, as a stream that lost them); `F` = derived frame id or `*`, `--seed` for the fraction mask;
`--shuffle-tiles SEED` (the maplets of every frame in a seeded random order, cells unchanged); `--late-tiles F:X,Y@G`
(tile (X, Y) moved out of frame F to the end of frame G > F, flagged late: the source emits it with F's event time).
`compare` leaves the cells of tiles missing from the input out of every stratum and reports how many there were.

Sliding windows in correctness runs: `WINDOW_FRAMES=2 WINDOW_SLIDE=1 scripts/correctness/case.sh ...` (`nes-query
--slide-frames`); `import-nes --windows-per-frame N` then requires every output tile exactly N times (tumbling: 1),
and `compare` fails on any multiplicity problem.

## Sources of truth

- **Tile store format.** `mabench/src/mabench/format.py` (Python) and `geotrellis-baseline/.../tilestore/Format.scala`
  (Scala) implement the same frozen layout (record version 4, `docs/maplet-format.md` §5); both fail fast on anything else.
- **Kept in sync by tests.** The Scala `FormatSuite` reads two small mabench stores (commands in
  `geotrellis-baseline/src/test/resources/fixtures/README.md`), re-writes them and requires the same SHA-256.

See `docs/architecture.md` for how each component works.
