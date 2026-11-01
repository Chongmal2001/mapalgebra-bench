"""Throughput runs -> one CSV row per run (``mabench perf-table``).

scripts/perf/run.sh leaves one directory per run with ``meta.json`` (system, store, workload, threads, rep, build)
next to the system's own output:

- NES (systems nes, nes-running, nes-kahan, and nes-passthrough: same source and Void sink, no focal window):
  ``status.json`` (nes-cli status; the timed span is running -> stopped, so worker start, warm-up query and query
  compilation are outside it), ``resources.csv`` (worker RSS and CPU ticks every ~100 ms, scripts/lib/sample.sh),
  ``worker_vmhwm`` (peak RSS over the worker's life) and ``worker_stat`` (clock ticks per second).
- GeoTrellis: ``summary.json`` of the micro-batch runner (elapsed time from the first frame's arrival to the last
  batch's end, after warm-up; ``compute_s`` = focal operation alone, ``buffer_s`` = its neighbour-buffering part,
  ``gc_s`` = GC time inside the span; process CPU, sampled RSS and used heap inside the span; per-batch latency).

Metrics: ``elapsed_s`` (formerly ``wall_s``: elapsed wall-clock time of the measured span), ``cpu_s`` (user + system
CPU of the process inside the span, all threads), ``peak_rss_bytes`` / ``mean_rss_bytes`` (sampled RSS inside the
span), ``rss_hwm_bytes`` (VmHWM over the process's life, incl. start-up and warm-up), ``peak_heap_bytes`` (GeoTrellis:
sampled total used heap inside the span), ``task_s`` (NES runs with the worker's event trace, NES_EVENT_TRACE=1: sum of
all task durations of the query's pipelines inside the span = thread time spent in the operators, source and sink
included; ``trace_dropped`` events the trace lost, then ``task_s`` undercounts). A run's ``variant`` (meta.json) is
appended to its system name, e.g. ``geotrellis-noavx``; ``rate_fps`` > 0 marks paced runs. Cells are counted the same way for both: records x bands x tile_size^2,
i.e. edge padding included.
"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

from .store import TileStore
from .workloads import parse_workload

COLUMNS = ["system", "threads", "rep", "store", "frames", "frames_per_window", "width", "height", "bands", "tile_size",
           "op", "shape", "radius", "rate_fps", "cells", "elapsed_s", "compute_s", "buffer_s", "gc_s", "cpu_s", "task_s",
           "trace_dropped",
           "throughput_cells_per_s", "cells_per_cpu_s", "peak_rss_bytes", "mean_rss_bytes", "rss_hwm_bytes",
           "peak_heap_bytes", "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "build", "host", "started_utc",
           "run_dir"]


def read_resources(path: Path) -> list[tuple[int, int, int]]:
    """(t_us, rss_kb, cpu_ticks) rows of a scripts/lib/sample.sh file; empty if missing."""
    if not path.exists():
        return []
    rows = []
    with open(path) as fh:
        for rec in csv.DictReader(fh):
            try:
                rows.append((int(rec["t_us"]), int(rec["rss_kb"]), int(rec["cpu_ticks"])))
            except (KeyError, TypeError, ValueError):
                continue                      # a row cut off when the sampler was stopped
    return rows


def _interp(samples: list[tuple[int, int, int]], t: int) -> float | None:
    """CPU ticks at time t, linear between the samples around it."""
    for (t0, _, c0), (t1, _, c1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return c0 + (c1 - c0) * ((t - t0) / (t1 - t0) if t1 > t0 else 0.0)
    return None


def span_resources(samples: list[tuple[int, int, int]], t0_us: int, t1_us: int, clk_tck: int) -> dict[str, Any]:
    """Peak / mean RSS and CPU seconds of a process inside [t0, t1] from its resource samples."""
    out: dict[str, Any] = {}
    inside = [s for s in samples if t0_us <= s[0] <= t1_us]
    if not inside:                            # span shorter than the sampling interval: nearest sample after t0
        inside = [s for s in samples if s[0] >= t0_us][:1]
    if inside:
        out["peak_rss_bytes"] = max(s[1] for s in inside) * 1024
        out["mean_rss_bytes"] = round(sum(s[1] for s in inside) / len(inside) * 1024)
    c0, c1 = _interp(samples, t0_us), _interp(samples, t1_us)
    if c0 is not None and c1 is not None:
        out["cpu_s"] = (c1 - c0) / clk_tck
    return out


def _nes(run: Path) -> dict[str, Any]:
    status = json.loads((run / "status.json").read_text())
    s = next((e for e in status if "local_query_id" in e), status[0]) if isinstance(status, list) else status
    if s.get("query_status") != "Stopped":
        raise ValueError(f"{run}: query_status {s.get('query_status')!r}")
    t0, t1 = s["running"]["since_epoch"], s["stopped"]["since_epoch"]      # µs
    row: dict[str, Any] = {"elapsed_s": (t1 - t0) / 1e6}
    hwm = run / "worker_vmhwm"
    if hwm.exists() and hwm.read_text().split():
        row["rss_hwm_bytes"] = int(hwm.read_text().split()[1]) * 1024
    clk = 100
    st = run / "worker_stat"
    if st.exists():
        for line in st.read_text().splitlines():
            if line.startswith("clk_tck "):
                clk = int(line.split()[1])
    row.update(span_resources(read_resources(run / "resources.csv"), t0, t1, clk))
    row.update(trace_tasks(run, t0, t1))
    return row


def trace_tasks(run: Path, t0_us: int, t1_us: int) -> dict[str, Any]:
    """Sum of task durations (µs -> s) in the worker's Chrome trace inside [t0, t1], per pipeline and in total. The
    trace may lack its closing lines when the worker was stopped, so it is read event by event."""
    traces = sorted(run.glob("trace_*.json"))
    if not traces:
        return {}
    total, per_pipeline = 0, {}
    for path in traces:
        for line in open(path):
            line = line.strip().rstrip(",")
            if not line.startswith("{") or '"dur"' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("cat") == "task" and e.get("ph") == "E" and t0_us <= e["ts"] <= t1_us:
                total += e["dur"]
                pid = e["args"]["pipeline_id"]
                per_pipeline[pid] = per_pipeline.get(pid, 0) + e["dur"]
    dropped = 0
    log = run / "worker.log"
    if log.exists():
        import re
        for m in re.finditer(r"Event queue full, (\d+) events dropped", log.read_text(errors="replace")):
            dropped = max(dropped, int(m.group(1)))
    return {"task_s": total / 1e6, "trace_dropped": dropped,
            "task_s_per_pipeline": {k: v / 1e6 for k, v in sorted(per_pipeline.items())}}


def _geotrellis(run: Path) -> dict[str, Any]:
    s = json.loads((run / "summary.json").read_text())
    lat = s.get("latency_ms", {})
    ms = lambda k: s[k] / 1e3 if s.get(k) is not None else None
    return {"elapsed_s": ms("elapsed_ms") if "elapsed_ms" in s else ms("wall_ms"), "cells": s["cells"],
            "compute_s": ms("compute_ms"), "buffer_s": ms("buffer_ms"), "gc_s": ms("gc_ms"), "cpu_s": ms("cpu_ms"),
            "peak_heap_bytes": s.get("peak_heap_used_bytes", s.get("peak_heap_bytes")),
            "peak_rss_bytes": s.get("peak_rss_bytes"), "mean_rss_bytes": s.get("mean_rss_bytes"),
            "rss_hwm_bytes": s.get("rss_hwm_bytes"),
            "latency_p50_ms": lat.get("p50"), "latency_p95_ms": lat.get("p95"), "latency_p99_ms": lat.get("p99")}


def collect(root: str | os.PathLike) -> list[dict[str, Any]]:
    manifests: dict[str, Any] = {}
    rows = []
    for meta_path in sorted(Path(root).rglob("meta.json")):
        run, meta = meta_path.parent, json.loads(meta_path.read_text())
        if meta["store"] not in manifests:
            manifests[meta["store"]] = TileStore.open(meta["store"]).manifest
        m = manifests[meta["store"]]
        p = parse_workload(meta["workload"]).params()
        variant = meta.get("variant") or ""
        row: dict[str, Any] = {
            "system": meta["system"] + (f"-{variant}" if variant else ""), "threads": meta["threads"], "rep": meta["rep"],
            "rate_fps": meta.get("rate_fps", 0),
            "store": os.path.basename(os.path.normpath(meta["store"])), "frames": len(m.frames),
            "frames_per_window": meta.get("frames_per_window", 1),
            "width": m.raster.width, "height": m.raster.height, "bands": m.raster.bands,
            "tile_size": m.tiling.tile_size, "op": p["op"], "shape": p["shape"], "radius": p["radius"],
            "cells": sum(f.n_records for f in m.frames) * m.raster.bands * m.tiling.tile_size ** 2,
            "build": meta.get("build", ""), "host": meta.get("host", ""), "started_utc": meta.get("started_utc", ""),
            "run_dir": str(run)}
        row.update((_geotrellis if meta["system"] == "geotrellis" else _nes)(run))
        if "task_s_per_pipeline" in row:
            (run / "task_times.json").write_text(json.dumps(row.pop("task_s_per_pipeline"), indent=2))
        row["throughput_cells_per_s"] = row["cells"] / row["elapsed_s"]
        if row.get("cpu_s"):
            row["cells_per_cpu_s"] = row["cells"] / row["cpu_s"]
        rows.append(row)
    rows.sort(key=lambda r: (r["store"], r["frames_per_window"], r["op"], r["shape"], r["radius"], r["system"],
                             r["threads"], r["rep"]))
    return rows


def write_table(root: str | os.PathLike, out: str | os.PathLike) -> int:
    rows = collect(root)
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows({k: "" if r.get(k) is None else r[k] for k in COLUMNS} for r in rows)
    return len(rows)
