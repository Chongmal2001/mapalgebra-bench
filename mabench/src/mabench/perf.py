"""Throughput runs -> one CSV row per run (``mabench perf-table``).

scripts/run-perf.sh leaves one directory per run with ``meta.json`` (system, store, workload, threads, rep, build)
next to the system's own output:

- NES (systems nes, nes-running, nes-kahan, and nes-passthrough: same source and Void sink, no focal window):
  ``status.json`` (nes-cli status; the timed span is running -> stopped, so container start and query compilation are
  outside it) and ``worker_vmhwm`` (peak RSS of the worker process).
- GeoTrellis: ``summary.json`` of the micro-batch runner (wall time from the first frame's arrival to the last
  batch's end, after warm-up; peak used heap; per-batch latency).

Cells are counted the same way for both: records x bands x tile_size^2, i.e. edge padding included.
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
           "op", "shape", "radius", "cells", "wall_s", "throughput_cells_per_s", "peak_mem_bytes", "mem_kind",
           "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "build", "host", "started_utc", "run_dir"]


def _nes(run: Path) -> dict[str, Any]:
    status = json.loads((run / "status.json").read_text())
    s = next((e for e in status if "local_query_id" in e), status[0]) if isinstance(status, list) else status
    if s.get("query_status") != "Stopped":
        raise ValueError(f"{run}: query_status {s.get('query_status')!r}")
    wall = (s["stopped"]["since_epoch"] - s["running"]["since_epoch"]) / 1e6
    row: dict[str, Any] = {"wall_s": wall, "mem_kind": "rss"}
    hwm = run / "worker_vmhwm"
    if hwm.exists() and hwm.read_text().split():
        row["peak_mem_bytes"] = int(hwm.read_text().split()[1]) * 1024
    return row


def _geotrellis(run: Path) -> dict[str, Any]:
    s = json.loads((run / "summary.json").read_text())
    lat = s.get("latency_ms", {})
    return {"wall_s": s["wall_ms"] / 1e3, "cells": s["cells"], "peak_mem_bytes": s["peak_heap_bytes"],
            "mem_kind": "jvm_heap_used", "latency_p50_ms": lat.get("p50"), "latency_p95_ms": lat.get("p95"),
            "latency_p99_ms": lat.get("p99")}


def collect(root: str | os.PathLike) -> list[dict[str, Any]]:
    manifests: dict[str, Any] = {}
    rows = []
    for meta_path in sorted(Path(root).rglob("meta.json")):
        run, meta = meta_path.parent, json.loads(meta_path.read_text())
        if meta["store"] not in manifests:
            manifests[meta["store"]] = TileStore.open(meta["store"]).manifest
        m = manifests[meta["store"]]
        p = parse_workload(meta["workload"]).params()
        row: dict[str, Any] = {
            "system": meta["system"], "threads": meta["threads"], "rep": meta["rep"],
            "store": os.path.basename(os.path.normpath(meta["store"])), "frames": len(m.frames),
            "frames_per_window": meta.get("frames_per_window", 1),
            "width": m.raster.width, "height": m.raster.height, "bands": m.raster.bands,
            "tile_size": m.tiling.tile_size, "op": p["op"], "shape": p["shape"], "radius": p["radius"],
            "cells": sum(f.n_records for f in m.frames) * m.raster.bands * m.tiling.tile_size ** 2,
            "build": meta.get("build", ""), "host": meta.get("host", ""), "started_utc": meta.get("started_utc", ""),
            "run_dir": str(run)}
        row.update((_geotrellis if meta["system"] == "geotrellis" else _nes)(run))
        row["throughput_cells_per_s"] = row["cells"] / row["wall_s"]
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
        w.writerows({k: r.get(k, "") for k in COLUMNS} for r in rows)
    return len(rows)
