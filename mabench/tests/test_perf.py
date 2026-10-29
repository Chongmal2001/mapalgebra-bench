import csv
import json

from mabench.cli import main
from mabench.perf import COLUMNS


def test_perf_table_reads_nes_and_geotrellis_runs(tmp_path):
    store = tmp_path / "s"
    assert main(["synth", "--out", str(store), "--width", "10", "--height", "6", "--bands", "2", "--frames", "3",
                 "--generator", "ramp", "--tile-size", "4"]) == 0
    cells = 3 * 6 * 2 * 16                       # frames x tiles (3 x 2) x bands x tile_size^2
    nes, gt = tmp_path / "perf" / "nes-t1" / "rep1", tmp_path / "perf" / "geotrellis-t1" / "rep1"
    for d, system in ((nes, "nes"), (gt, "geotrellis")):
        d.mkdir(parents=True)
        (d / "meta.json").write_text(json.dumps({"system": system, "store": str(store), "workload": "focal:max:circle:2",
                                                 "threads": 1, "rep": 1, "build": "Release"}))
    (nes / "status.json").write_text(json.dumps([
        {"query_status": "Stopped", "running": {"since_epoch": 1_000_000}, "stopped": {"since_epoch": 3_000_000}},
        {"local_query_id": "x", "query_status": "Stopped", "running": {"since_epoch": 1_000_000},
         "stopped": {"since_epoch": 3_000_000}}]))
    (nes / "worker_vmhwm").write_text("VmHWM:\t    2048 kB\n")
    (gt / "summary.json").write_text(json.dumps({"wall_ms": 500.0, "cells": cells, "peak_heap_bytes": 99,
                                                 "latency_ms": {"p50": 1.0, "p95": 2.0, "p99": 3.0}}))
    assert main(["perf-table", str(tmp_path / "perf"), "--out", str(tmp_path / "perf.csv")]) == 0
    rows = {r["system"]: r for r in csv.DictReader(open(tmp_path / "perf.csv"))}
    assert list(rows["nes"]) == COLUMNS
    assert int(rows["nes"]["cells"]) == int(rows["geotrellis"]["cells"]) == cells
    assert float(rows["nes"]["wall_s"]) == 2.0 and float(rows["nes"]["throughput_cells_per_s"]) == cells / 2
    assert int(rows["nes"]["peak_mem_bytes"]) == 2048 * 1024 and rows["nes"]["mem_kind"] == "rss"
    assert float(rows["geotrellis"]["throughput_cells_per_s"]) == cells / 0.5
    assert (rows["geotrellis"]["op"], rows["geotrellis"]["shape"], rows["geotrellis"]["radius"]) == ("max", "circle", "2")
