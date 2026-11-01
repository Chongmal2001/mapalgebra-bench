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
    (nes / "worker_stat").write_text("1 (nes) S 0 0 0\nclk_tck 100\n")
    # samples every 0.5 s; the span 1.0 s .. 3.0 s holds the samples at 1.0, 1.5, ..., 3.0 s
    (nes / "resources.csv").write_text("t_us,rss_kb,cpu_ticks\n" + "".join(
        f"{t * 500_000},{1000 + 100 * t},{50 * t}\n" for t in range(0, 8)) + "7,")
    (gt / "summary.json").write_text(json.dumps({"elapsed_ms": 500.0, "compute_ms": 400.0, "buffer_ms": 40.0,
                                                 "gc_ms": 20, "cpu_ms": 1000.0, "cells": cells,
                                                 "peak_heap_used_bytes": 99, "peak_rss_bytes": 5000,
                                                 "rss_hwm_bytes": 6000,
                                                 "latency_ms": {"p50": 1.0, "p95": 2.0, "p99": 3.0}}))
    assert main(["perf-table", str(tmp_path / "perf"), "--out", str(tmp_path / "perf.csv")]) == 0
    rows = {r["system"]: r for r in csv.DictReader(open(tmp_path / "perf.csv"))}
    assert list(rows["nes"]) == COLUMNS
    assert int(rows["nes"]["cells"]) == int(rows["geotrellis"]["cells"]) == cells
    assert float(rows["nes"]["elapsed_s"]) == 2.0 and float(rows["nes"]["throughput_cells_per_s"]) == cells / 2
    assert int(rows["nes"]["rss_hwm_bytes"]) == 2048 * 1024
    assert int(rows["nes"]["peak_rss_bytes"]) == (1000 + 100 * 6) * 1024          # last sample inside the span
    assert int(rows["nes"]["mean_rss_bytes"]) == (1000 + 100 * 4) * 1024          # samples 2..6
    assert float(rows["nes"]["cpu_s"]) == 2.0                                      # 200 ticks at 100 per second
    assert float(rows["nes"]["cells_per_cpu_s"]) == cells / 2
    assert float(rows["geotrellis"]["throughput_cells_per_s"]) == cells / 0.5
    assert (rows["geotrellis"]["op"], rows["geotrellis"]["shape"], rows["geotrellis"]["radius"]) == ("max", "circle", "2")
    assert float(rows["geotrellis"]["compute_s"]) == 0.4 and float(rows["geotrellis"]["gc_s"]) == 0.02
    assert float(rows["geotrellis"]["buffer_s"]) == 0.04 and float(rows["geotrellis"]["cpu_s"]) == 1.0
    assert int(rows["geotrellis"]["peak_heap_bytes"]) == 99 and int(rows["geotrellis"]["peak_rss_bytes"]) == 5000
    assert rows["nes"]["compute_s"] == "" and rows["nes"]["gc_s"] == "" and rows["nes"]["peak_heap_bytes"] == ""


def test_span_resources_short_span_and_legacy_summary(tmp_path):
    from mabench.perf import _geotrellis, span_resources
    samples = [(0, 100, 0), (100_000, 200, 10), (200_000, 300, 20)]
    r = span_resources(samples, 120_000, 150_000, 100)      # no sample inside: nearest after the start
    assert r["peak_rss_bytes"] == 300 * 1024 and abs(r["cpu_s"] - 0.03) < 1e-12
    (tmp_path / "summary.json").write_text(json.dumps({"wall_ms": 2000.0, "cells": 1, "peak_heap_bytes": 7}))
    g = _geotrellis(tmp_path)
    assert g["elapsed_s"] == 2.0 and g["peak_heap_bytes"] == 7 and g["cpu_s"] is None


def test_event_trace_variant_and_rate(tmp_path):
    store = tmp_path / "s"
    assert main(["synth", "--out", str(store), "--width", "8", "--height", "8", "--frames", "1", "--generator", "ramp",
                 "--tile-size", "4"]) == 0
    run = tmp_path / "perf" / "nes-t1-trace" / "rep1"
    run.mkdir(parents=True)
    (run / "meta.json").write_text(json.dumps({"system": "nes", "store": str(store), "workload": "focal:max:square:1",
                                               "threads": 1, "rep": 1, "variant": "trace", "rate_fps": 0}))
    (run / "status.json").write_text(json.dumps({"query_status": "Stopped", "running": {"since_epoch": 1000},
                                                 "stopped": {"since_epoch": 9000}}))
    ev = lambda ts, dur, p: json.dumps({"args": {"pipeline_id": p, "task_id": 1}, "cat": "task", "dur": dur,
                                        "name": "Task", "ph": "E", "pid": 1, "tid": 2, "ts": ts})
    (run / "trace_x.json").write_text('{\n  "traceEvents": [\n'
                                      + ",\n".join(["    " + ev(500, 400, 1), "    " + ev(2000, 1000, 2),
                                                    "    " + ev(8000, 3000, 3), "    " + ev(9500, 7, 3)]))   # unterminated
    (run / "worker.log").write_text("x Event queue full, 1 events dropped so far\ny Event queue full, 100 events dropped\n")
    gt = tmp_path / "perf" / "gt-noavx" / "rep1"
    gt.mkdir(parents=True)
    (gt / "meta.json").write_text(json.dumps({"system": "geotrellis", "store": str(store), "workload": "focal:max:square:1",
                                              "threads": 1, "rep": 1, "variant": "noavx", "rate_fps": 0.5}))
    (gt / "summary.json").write_text(json.dumps({"elapsed_ms": 10.0, "cells": 64, "latency_ms": {"p50": 4.0}}))
    assert main(["perf-table", str(tmp_path / "perf"), "--out", str(tmp_path / "perf.csv")]) == 0
    rows = {r["system"]: r for r in csv.DictReader(open(tmp_path / "perf.csv"))}
    assert float(rows["nes-trace"]["task_s"]) == 0.004 and int(rows["nes-trace"]["trace_dropped"]) == 100
    assert json.loads((run / "task_times.json").read_text()) == {"2": 0.001, "3": 0.003}
    assert float(rows["geotrellis-noavx"]["rate_fps"]) == 0.5
