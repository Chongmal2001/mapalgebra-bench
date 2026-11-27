from __future__ import annotations

import csv
import json
import math
import os
from pathlib import Path
from typing import Any

from .store import TileStore
from .workloads import parse_workload

COLUMNS = ["system", "threads", "rep", "store", "frames", "frames_per_window", "slide_frames", "width", "height", "bands", "tile_size",
           "op", "shape", "radius", "rate_fps", "cells", "cells_padded", "elapsed_s", "compute_s", "buffer_s", "gc_s", "cpu_s", "task_s",
           "trace_dropped",
           "throughput_cells_per_s", "cells_per_cpu_s", "peak_rss_bytes", "mean_rss_bytes", "rss_hwm_bytes",
           "latency_p50_ms", "latency_p95_ms", "latency_p99_ms", "frame_latency_p50_ms",
           "frame_latency_p95_ms", "frame_latency_p99_ms", "ttfo_p50_ms", "ttfo_p95_ms", "release_lag_max_ms", "build",
           "host", "started_utc", "run_dir"]


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[max(0, math.ceil(q * len(v)) - 1)]


def _latency_columns(window: list[float], frame: list[float], first: list[float]) -> dict[str, Any]:
    return {"latency_p50_ms": pct(window, 0.5), "latency_p95_ms": pct(window, 0.95), "latency_p99_ms": pct(window, 0.99),
            "frame_latency_p50_ms": pct(frame, 0.5), "frame_latency_p95_ms": pct(frame, 0.95),
            "frame_latency_p99_ms": pct(frame, 0.99), "ttfo_p50_ms": pct(first, 0.5), "ttfo_p95_ms": pct(first, 0.95)}


def nes_paced(run: Path, event_ms: dict[int, int], window_ms: int) -> dict[str, Any]:
    pacing, outputs = run / "pacing.csv", run / "frame-latency.csv"
    if not (pacing.exists() and outputs.exists()):
        return {}
    rel = {int(r["src_frame_id"]): (int(r["due_ns"]), int(r["release_ns"])) for r in csv.DictReader(open(pacing))}
    out = {int(r["src_frame_id"]): (int(r["first_ns"]), int(r["last_ns"])) for r in csv.DictReader(open(outputs))}
    if missing := sorted(set(rel) - set(out)):
        raise ValueError(f"{run}: no outputs for released frames {missing[:10]}")
    ms = 1e-6
    frame = [(out[f][1] - rel[f][0]) * ms for f in rel]
    first = [(out[f][0] - rel[f][0]) * ms for f in rel]
    windows: dict[int, list[int]] = {}
    for f in rel:
        windows.setdefault(event_ms[f] // window_ms, []).append(f)
    window = [(max(out[f][1] for f in fs) - max(rel[f][0] for f in fs)) * ms for fs in windows.values()]
    return {**_latency_columns(window, frame, first),
            "release_lag_max_ms": max((r - d) * ms for d, r in rel.values()) if rel else None}


def geotrellis_paced(run: Path, rate_fps: float) -> dict[str, Any]:
    path = run / "batches.csv"
    if rate_fps <= 0 or not path.exists():
        return {}
    frame = []
    for b in csv.DictReader(open(path)):
        n, last, done = int(float(b["frames"])), float(b["last_arrival_ms"]), float(b["done_ms"])
        frame += [done - (last - (n - 1 - j) * 1e3 / rate_fps) for j in range(n)]
    window = [float(b["latency_ms"]) for b in csv.DictReader(open(path))]
    return _latency_columns(window, frame, frame)


def real_cells(m) -> int:
    tiles = m.tiling.tiles_x * m.tiling.tiles_y
    per_frame = m.raster.width * m.raster.height * m.raster.bands
    return round(sum(per_frame * min(f.n_records, tiles) / tiles for f in m.frames))


def read_resources(path: Path) -> list[tuple[int, int, int]]:
    if not path.exists():
        return []
    rows = []
    with open(path) as fh:
        for rec in csv.DictReader(fh):
            try:
                rows.append((int(rec["t_us"]), int(rec["rss_kb"]), int(rec["cpu_ticks"])))
            except (KeyError, TypeError, ValueError):
                continue
    return rows


def _interp(samples: list[tuple[int, int, int]], t: int) -> float | None:
    for (t0, _, c0), (t1, _, c1) in zip(samples, samples[1:]):
        if t0 <= t <= t1:
            return c0 + (c1 - c0) * ((t - t0) / (t1 - t0) if t1 > t0 else 0.0)
    return None


def span_resources(samples: list[tuple[int, int, int]], t0_us: int, t1_us: int, clk_tck: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    inside = [s for s in samples if t0_us <= s[0] <= t1_us]
    if not inside:
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
    t0, t1 = s["running"]["since_epoch"], s["stopped"]["since_epoch"]
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
    traces = sorted(run.glob("trace_*.json"))
    if not traces:
        return {}
    total, per_pipeline, starts, ends = 0, {}, set(), set()
    for path in traces:
        for line in open(path):
            line = line.strip().rstrip(",")
            if not line.startswith("{") or '"task' not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("cat") != "task" or not t0_us <= e.get("ts", -1) <= t1_us:
                continue
            tid = (path.name, e["args"].get("task_id"))
            if e.get("ph") == "B":
                starts.add(tid)
            elif e.get("ph") == "E":
                ends.add(tid)
                total += e["dur"]
                pid = e["args"]["pipeline_id"]
                per_pipeline[pid] = per_pipeline.get(pid, 0) + e["dur"]
    dropped = len(starts ^ ends)
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
    return {"elapsed_s": ms("elapsed_ms") if "elapsed_ms" in s else ms("wall_ms"),
            "compute_s": ms("compute_ms"), "buffer_s": ms("buffer_ms"), "gc_s": ms("gc_ms"), "cpu_s": ms("cpu_ms"),
            "latency_p50_ms": lat.get("p50"), "latency_p95_ms": lat.get("p95"), "latency_p99_ms": lat.get("p99")}


def _store_manifest(root: Path, store: str):
    name = os.path.basename(os.path.normpath(store))
    candidates = [Path(store)]
    candidates += [p / "stores" / name for p in root.resolve().parents]
    candidates += [p / "manifests" / name for p in [root.resolve(), *root.resolve().parents]]
    for c in candidates:
        if (c / "manifest.json").is_file():
            return TileStore.open(str(c)).manifest
    raise FileNotFoundError(f"no manifest for store {name} (recorded path {store}); copy it to "
                            f"<result set>/manifests/{name}/manifest.json")


def collect(root: str | os.PathLike) -> list[dict[str, Any]]:
    manifests: dict[str, Any] = {}
    rows = []
    for meta_path in sorted(Path(root).rglob("meta.json")):
        run, meta = meta_path.parent, json.loads(meta_path.read_text())
        if meta["store"] not in manifests:
            manifests[meta["store"]] = _store_manifest(Path(root), meta["store"])
        m = manifests[meta["store"]]
        p = parse_workload(meta["workload"]).params()
        variant = meta.get("variant") or ""
        fpw = int(meta.get("frames_per_window", 1))
        slide = int(meta.get("slide_frames", fpw))
        row: dict[str, Any] = {
            "system": meta["system"] + (f"-{variant}" if variant else "") + (f"-s{slide}" if slide != fpw else ""),
            "threads": meta["threads"], "rep": meta["rep"], "slide_frames": slide,
            "rate_fps": meta.get("rate_fps", 0),
            "store": os.path.basename(os.path.normpath(meta["store"])), "frames": len(m.frames),
            "frames_per_window": meta.get("frames_per_window", 1),
            "width": m.raster.width, "height": m.raster.height, "bands": m.raster.bands,
            "tile_size": m.tiling.tile_size, "op": p["op"], "shape": p["shape"], "radius": p["radius"],
            "cells": real_cells(m), "cells_padded": sum(f.n_records for f in m.frames) * m.raster.bands * m.tiling.tile_size ** 2,
            "build": meta.get("build", ""), "host": meta.get("host", ""), "started_utc": meta.get("started_utc", ""),
            "run_dir": str(run)}
        row.update((_geotrellis if meta["system"] == "geotrellis" else _nes)(run))
        rate = float(meta.get("rate_fps", 0) or 0)
        if meta["system"] == "geotrellis":
            row.update(geotrellis_paced(run, rate))
        elif rate > 0:
            times = sorted(f.event_time_ms for f in m.frames)
            step = min([b - a for a, b in zip(times, times[1:]) if b > a] or [1000])
            row.update(nes_paced(run, {f.frame_id: f.event_time_ms for f in m.frames},
                                 step * int(meta.get("frames_per_window", 1))))
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
