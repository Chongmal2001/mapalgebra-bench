import csv
import json

from mabench.cli import main
from mabench.perf import COLUMNS
from mabench.report import aggregate, load_correctness, load_perf


def _perf_csv(path):
    rows = []
    for fpw in (1, 4):
        for op in ("max", "mean"):
            systems = ["nes", "geotrellis"] + (["nes-running", "nes-kahan"] if op == "mean" else [])
            for r in (1, 2, 4):
                for sys in systems:
                    for rep in (1, 2, 3):
                        wall = {"nes": 1.0 * r * r, "geotrellis": 2.0 * r, "nes-running": 0.5,
                                "nes-kahan": 0.6}[sys] + 0.01 * rep
                        rows.append({"system": sys, "threads": 1, "rep": rep, "store": "s", "frames": 4,
                                     "frames_per_window": fpw, "width": 10, "height": 10, "bands": 1, "tile_size": 4,
                                     "op": op, "shape": "square", "radius": r, "cells": 1_000_000, "elapsed_s": wall,
                                     "compute_s": wall * 0.8 if sys == "geotrellis" else "",
                                     "gc_s": 0.05 if sys == "geotrellis" else "",
                                     "throughput_cells_per_s": 1_000_000 / wall, "peak_rss_bytes": 2**30,
                                     "cpu_s": wall, "peak_heap_bytes": 2**29 if sys == "geotrellis" else ""})
        rows.append({"system": "nes-passthrough", "threads": 1, "rep": 1, "store": "s", "frames": 4,
                     "frames_per_window": fpw, "op": "max", "shape": "square", "radius": 1, "cells": 1_000_000,
                     "elapsed_s": 0.1, "throughput_cells_per_s": 1e7, "peak_rss_bytes": 2**29})
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows({k: r.get(k, "") for k in COLUMNS} for r in rows)


def test_aggregate_takes_median_min_max(tmp_path):
    _perf_csv(tmp_path / "perf.csv")
    agg = aggregate(load_perf(tmp_path / "perf.csv"), "elapsed_s")
    med, lo, hi, n = agg[("s", "max", "square", 2, 1, "nes", 1)]
    assert (round(med, 2), round(lo, 2), round(hi, 2), n) == (4.02, 4.01, 4.03, 3)


def test_report_writes_figures_and_tables(tmp_path):
    _perf_csv(tmp_path / "perf.csv")
    res = tmp_path / "results" / "s"
    res.mkdir(parents=True)
    tot = {"data_both": 10, "exact_rate": 1.0, "tolerance_rate": 1.0, "max_ulp": 0.0, "max_abs_error": 0.0,
           "nodata_only_candidate": 0, "nodata_only_reference": 0}
    for case, ok in (("max-square-4", True), ("mean-square-4-kahan-w4", True), ("sum-circle-2", False)):
        (res / f"{case}.json").write_text(json.dumps({"passed": ok, "criterion": "exact", "coverage_problems": [],
                                                      "totals": {"all": tot}}))
    (res / "notes.json").write_text("{}")                          # ignored: not a case name
    crows = load_correctness(tmp_path / "results")
    assert [(c["mode"], c["fpw"]) for c in crows] == [("exact", 1), ("kahan", 4), ("exact", 1)]
    out = tmp_path / "report"
    assert main(["report", "--perf", str(tmp_path / "perf.csv"), "--results", str(tmp_path / "results"),
                 "--out", str(out), "--format", "png"]) == 0
    names = {p.name for p in out.iterdir()}
    for f in ("throughput_s_square.png", "speedup_s_square.png", "window_s_square.png", "memory_s_square.png",
              "breakdown_s_max_square.png", "correctness_matrix.png", "report.md"):
        assert f in names, f
    md = (out / "report.md").read_text()
    assert "2/3 PASS" in md and "| s | mean | square | 4 | 4 | nes-kahan | 1 | 3 |" in md


def test_legacy_perf_csv_and_timeline(tmp_path):
    old = ["system", "threads", "rep", "store", "frames", "frames_per_window", "op", "shape", "radius", "cells",
           "wall_s", "throughput_cells_per_s", "peak_mem_bytes", "mem_kind", "run_dir"]
    nes, gt = tmp_path / "nes", tmp_path / "gt"
    for d in (nes, gt):
        d.mkdir()
        (d / "resources.csv").write_text("t_us,rss_kb,cpu_ticks\n" + "".join(
            f"{1_000_000 + 100_000 * i},{1000 + 50 * i},{i}\n" for i in range(30)))
    (nes / "status.json").write_text(json.dumps({"query_status": "Stopped", "running": {"since_epoch": 1_200_000},
                                                 "stopped": {"since_epoch": 3_500_000}}))
    (gt / "summary.json").write_text(json.dumps({"span_start_us": 1_100_000, "span_end_us": 3_000_000}))
    with open(tmp_path / "perf.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(old)
        w.writerow(["nes", 1, 1, "s", 2, 1, "max", "square", 4, 100, 2.0, 50, 2**30, "rss", str(nes)])
        w.writerow(["geotrellis", 1, 1, "s", 2, 1, "max", "square", 4, 100, 4.0, 25, 2**29, "jvm_heap_used", str(gt)])
    rows = load_perf(tmp_path / "perf.csv")
    by = {r["system"]: r for r in rows}
    assert by["nes"]["elapsed_s"] == 2.0 and by["nes"]["rss_hwm_bytes"] == 2**30
    assert by["geotrellis"]["peak_heap_bytes"] == 2**29 and by["geotrellis"]["rss_hwm_bytes"] is None
    out = tmp_path / "report"
    assert main(["report", "--perf", str(tmp_path / "perf.csv"), "--results", str(tmp_path / "none"),
                 "--out", str(out), "--format", "png"]) == 0
    names = {p.name for p in out.iterdir()}
    assert "timeline_s_max_square.png" in names and "memory_s_square.png" in names


def test_sliding_case_names_and_tolerance_column(tmp_path):
    res = tmp_path / "results" / "s"
    res.mkdir(parents=True)
    tot = {"data_both": 10, "exact_rate": 0.5, "tolerance_rate": 1.0, "max_ulp": 3.0, "max_abs_error": 1e-13,
           "nodata_only_candidate": 0, "nodata_only_reference": 0, "max_tol_util": 0.25}
    (res / "mean-square-4-kahan-w2s1.json").write_text(json.dumps(
        {"passed": True, "criterion": "tolerance", "coverage_problems": [], "missing_input_tiles": {"0": 2},
         "totals": {"all": tot}}))
    (c,) = load_correctness(tmp_path / "results")
    assert (c["mode"], c["fpw"], c["slide"], c["tol_util"], c["missing_input_tiles"]) == ("kahan", 2, 1, 0.25, 2)
    out = tmp_path / "report"
    assert main(["report", "--perf", str(tmp_path / "none.csv"), "--results", str(tmp_path / "results"),
                 "--out", str(out), "--format", "png"]) == 0
    assert "| 0.25 | 0 | 2 | ok |" in (out / "report.md").read_text()


def test_case_id_layout(tmp_path):
    tot = {"data_both": 1, "exact_rate": 1.0, "tolerance_rate": 1.0, "max_ulp": 0.0, "max_abs_error": 0.0,
           "nodata_only_candidate": 0, "nodata_only_reference": 0}
    for case, store, run in (("K2", "st-t16", "max-square-20"), ("K9", "st4", "max-square-4-t16"),
                             ("K3", "st4", "sum-circle-1-w2s1")):
        d = tmp_path / case / store
        d.mkdir(parents=True)
        (d / f"{run}.json").write_text(json.dumps({"passed": True, "criterion": "exact", "coverage_problems": [],
                                                   "totals": {"all": tot}}))
    rows = sorted(load_correctness(tmp_path), key=lambda c: c["case_id"])
    assert [(c["case_id"], c["store"], c["tag"], c["fpw"], c["slide"]) for c in rows] == \
        [("K2", "st-t16", "", 1, 1), ("K3", "st4", "", 2, 1), ("K9", "st4", "t16", 1, 1)]
