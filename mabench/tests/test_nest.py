import random
from dataclasses import replace

import numpy as np
import pytest

from mabench import format as fmt
from mabench import oracle
from mabench.cli import main
from mabench.nes_dump import DUMP_HEADER_LINE, DUMP_RECORD, nes_query
from mabench.store import TileStore
from mabench.workloads import FocalWorkload, parse_workload, workload_from_dict


@pytest.fixture(scope="module")
def store(tmp_path_factory):
    root = tmp_path_factory.mktemp("nest")
    assert main(["synth", "--out", str(root / "st"), "--width", "37", "--height", "29", "--generator", "normal",
                 "--bands", "2", "--frames", "2", "--frame-period-ms", "100", "--tile-size", "8", "--seed", "4",
                 "--param", "nodata_fraction=0.05", "--param", "nodata_block=6,6,5,4"]) == 0
    return root


def _dump_from(ref: TileStore, path):
    recs = [(1, i.frame_id, i.event_time_ms, fmt.encode_maplet(replace(h, frame_id=i.frame_id + 1), c))
            for i in ref.manifest.frames for h, c in ref.records(i.frame_id)]
    random.Random(3).shuffle(recs)
    with open(path, "wb") as fh:
        fh.write(DUMP_HEADER_LINE)
        for a, b, t, m in recs:
            fh.write(DUMP_RECORD.pack(a, b, t, len(m)) + m)
    return str(path)


def test_workload_serialisation():
    single = parse_workload("focal:mean:square:1")
    assert single.nest == 1 and single.to_dict()["params"] == {"op": "mean", "shape": "square", "radius": 1}
    assert single.name == "focal:mean:square:1"
    nested = replace(single, nest=2)
    assert nested.to_dict()["params"]["nest"] == 2 and nested.reach == 2
    assert workload_from_dict(nested.to_dict()) == nested
    assert parse_workload("focal:mean:square:1:2") == nested
    with pytest.raises(ValueError):
        FocalWorkload("mean", "square", 1, nest=0)


def test_nested_footprint():
    sq = oracle.footprint("square", 1)
    assert np.array_equal(oracle.footprint_nested(sq, 2), oracle.footprint("square", 2))
    assert np.array_equal(oracle.footprint_nested(sq, 1), sq)
    c2 = oracle.footprint_nested(oracle.footprint("circle", 1), 2)
    dy, dx = np.mgrid[-2:3, -2:3]
    assert np.array_equal(c2, np.abs(dy) + np.abs(dx) <= 2)


def test_nested_oracle_is_the_naive_definition_applied_twice():
    a = np.random.default_rng(0).normal(size=(9, 11))
    a[2, 3] = a[6, 7] = np.nan
    for op in oracle.OPS:
        for shape in oracle.SHAPES:
            fp = oracle.footprint(shape, 1)
            want = oracle.naive_focal(oracle.naive_focal(a, op, fp), op, fp)
            np.testing.assert_allclose(oracle.focal_nested(a, op, fp, 2), want, rtol=1e-12, atol=1e-12)


def test_nested_query(store):
    st = TileStore.open(str(store / "st"))
    single = nes_query(st, parse_workload("focal:max:circle:1"), "/x/d.bin")
    nested = nes_query(st, FocalWorkload("max", "circle", 1, nest=2), "/x/d.bin")
    call = "FOCAL_MAX(raster, DISC, 1)"
    assert single.count(call) == 1 and nested.count(call) == 2
    assert "\n".join([
        "SELECT srcId, srcFrameId, ts, raster FROM (",
        f"    SELECT ts, srcId, srcFrameId, {call} AS raster FROM (",
        f"        SELECT ts, srcId, srcFrameId, {call} AS raster",
        "        FROM tiles WINDOW TUMBLING(ts, size 100 ms))",
        "    WINDOW TUMBLING(ts, size 100 ms)) INTO dump;"]) in nested
    assert single.split("SELECT srcId")[0] == nested.split("SELECT srcId")[0]


@pytest.mark.parametrize("op,shape", [(o, s) for o in oracle.OPS for s in oracle.SHAPES])
def test_strip_reference_equals_whole_frame_nested_oracle(store, tmp_path, op, shape):
    ref = tmp_path / "ref"
    assert main(["reference", str(store / "st"), "--workload", f"focal:{op}:{shape}:2", "--nest", "2",
                 "--out", str(ref)]) == 0
    st, R = TileStore.open(str(store / "st")), TileStore.open(str(ref))
    assert R.manifest.provenance["workload"]["params"]["nest"] == 2
    fp = oracle.footprint(shape, 2)
    for f in st.manifest.frames:
        x = st.assemble(f.frame_id)
        want = np.stack([oracle.focal_nested(x[b], op, fp, 2) for b in range(x.shape[0])])
        np.testing.assert_array_equal(R.assemble(f.frame_id), want)


def test_import_and_compare_nested_end_to_end(store, tmp_path, capsys):
    wl, st = "focal:mean:square:1", str(store / "st")
    ref, ref1 = tmp_path / "ref2", tmp_path / "ref1"
    assert main(["reference", st, "--workload", wl, "--nest", "2", "--out", str(ref)]) == 0
    assert main(["reference", st, "--workload", wl, "--out", str(ref1)]) == 0
    out = tmp_path / "nes"
    dump = _dump_from(TileStore.open(str(ref)), tmp_path / "d.bin")
    assert main(["import-nes", dump, "--input", st, "--workload", wl, "--nest", "2", "--out", str(out),
                 "--frames-per-window", "1"]) == 0
    assert main(["compare", str(out), "--reference", str(ref), "--input", st, "--nest", "2"]) == 0
    assert "2 * 1e-12 * op^2(|v|)" in capsys.readouterr().out
    once = tmp_path / "once"
    assert main(["import-nes", _dump_from(TileStore.open(str(ref1)), tmp_path / "d1.bin"), "--input", st,
                 "--workload", wl, "--nest", "2", "--out", str(once), "--frames-per-window", "1"]) == 0
    assert main(["compare", str(once), "--reference", str(ref), "--input", st, "--nest", "2"]) == 1
    with pytest.raises(ValueError, match="produced by workload"):
        main(["compare", str(out), "--reference", str(ref1), "--input", st])


def test_nested_tolerance_accepts_rounding_and_rejects_real_errors(store, tmp_path):
    from mabench.compare import compare_focal
    st = TileStore.open(str(store / "st"))
    ref = tmp_path / "ref"
    assert main(["reference", str(store / "st"), "--workload", "focal:sum:square:1", "--nest", "2", "--out", str(ref)]) == 0
    R = TileStore.open(str(ref))

    def candidate(scale, name):
        recs = [(1, i.frame_id, i.event_time_ms, fmt.encode_maplet(replace(h, frame_id=i.frame_id + 1), c * scale))
                for i in R.manifest.frames for h, c in R.records(i.frame_id)]
        path = tmp_path / f"{name}.bin"
        with open(path, "wb") as fh:
            fh.write(DUMP_HEADER_LINE)
            for a, b, t, m in recs:
                fh.write(DUMP_RECORD.pack(a, b, t, len(m)) + m)
        out = tmp_path / name
        assert main(["import-nes", str(path), "--input", str(store / "st"), "--workload", "focal:sum:square:1",
                     "--nest", "2", "--out", str(out)]) == 0
        return TileStore.open(str(out))

    near = compare_focal(st, R, candidate(1 + 1e-14, "near"), "sum", "square", 1, nest=2)
    far = compare_focal(st, R, candidate(1 + 1e-9, "far"), "sum", "square", 1, nest=2)
    assert near.passed and near.totals()["all"].max_tol_util < 1
    assert not far.passed


def test_report_parses_nested_run_names(tmp_path):
    from mabench.report import CASE
    m = CASE.match("mean-square-1-kahan-n2")
    assert (m["op"], m["mode"], m["n"], m["tag"]) == ("mean", "kahan", "2", None)
    m = CASE.match("max-square-1-t16")
    assert (m["n"], m["tag"]) == (None, "t16")
