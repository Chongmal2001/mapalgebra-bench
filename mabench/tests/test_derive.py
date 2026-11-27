import json

import numpy as np
import pytest

from mabench.cli import main
from mabench.store import TileStore


@pytest.fixture()
def store(tmp_path):
    p = tmp_path / "in"
    assert main(["synth", "--out", str(p), "--width", "20", "--height", "13", "--generator", "ramp", "--bands", "3",
                 "--frames", "4", "--frame-period-ms", "1000", "--tile-size", "8"]) == 0
    return TileStore.open(p)


def _derive(tmp_path, store, *args, name="out"):
    out = tmp_path / name
    assert main(["derive", str(store.location), "--out", str(out), *args]) == 0
    return TileStore.open(out)


def test_frames_bands_scale(tmp_path, store):
    d = _derive(tmp_path, store, "--frames", "1:3", "--bands", "3,1", "--scale", "0.5", "--offset", "-1")
    m, mi = d.manifest, store.manifest
    assert [f.frame_id for f in m.frames] == [0, 1] and m.raster.bands == 2
    assert [f.event_time_ms for f in m.frames] == [mi.frames[1].event_time_ms, mi.frames[2].event_time_ms]
    assert [f.source["derived_from_frame"] for f in m.frames] == [1, 2]
    a, b = d.assemble(0), store.assemble(1)
    np.testing.assert_array_equal(a, b[[2, 0]] * 0.5 - 1)
    assert d.verify().ok
    assert m.provenance["derive"]["bands"] == [3, 1] and m.provenance["input_manifest_sha256"]


def test_nan_patterns(tmp_path, store):
    d = _derive(tmp_path, store, "--nan", "0:block=6,5,4,4", "--nan", "1:tile=2,1", "--nan", "*:fraction=0.25",
                "--seed", "7")
    f0, f1, f2 = d.assemble(0), d.assemble(1), d.assemble(2)
    assert np.isnan(f0[:, 5:9, 6:10]).all()
    assert np.isnan(f1[:, 8:13, 16:20]).all()
    frac = np.isnan(f2[0]).mean()
    assert 0.15 < frac < 0.35
    assert (np.isnan(f2[0]) == np.isnan(f2[2])).all()
    again = _derive(tmp_path, store, "--nan", "*:fraction=0.25", "--seed", "7", name="again")
    assert (np.isnan(again.assemble(2)) == np.isnan(f2)).all()
    for f in d.manifest.frames:
        assert f.valid_cells == int((~np.isnan(d.assemble(f.frame_id))).sum())


def test_drop_tiles_is_legal_framing(tmp_path, store):
    d = _derive(tmp_path, store, "--drop-tiles", "0:1,0", "--drop-tiles", "2:row=1", "--drop-tiles", "*:col=2",
                "--frames", "0:3")
    keys = [set(d.keys(f.frame_id)) for f in d.manifest.frames]
    assert keys[0] == {(0, 0), (0, 1), (1, 1)}
    assert keys[1] == {(0, 0), (1, 0), (0, 1), (1, 1)}
    assert keys[2] == {(0, 0), (1, 0)}
    assert [f.n_records for f in d.manifest.frames] == [3, 4, 2]
    assert np.isnan(d.assemble(0)[:, 0:8, 8:16]).all()
    rep = d.verify()
    assert not rep.ok and all("tiles missing" in p for p in rep.problems)
    assert d.manifest.provenance["derive"]["tiles_dropped"] == 1 + 2 + 3 * 2


def test_shuffle_tiles_keeps_cells(tmp_path, store):
    d = _derive(tmp_path, store, "--shuffle-tiles", "3", "--drop-tiles", "1:0,0")
    row_major = [(x, y) for y in range(2) for x in range(3)]
    keys = [d.keys(f.frame_id) for f in d.manifest.frames]
    assert any(k != row_major for k in keys)
    assert sorted(keys[0], key=lambda k: (k[1], k[0])) == row_major
    assert set(keys[1]) == set(row_major) - {(0, 0)}
    for f in (0, 2, 3):
        np.testing.assert_array_equal(d.assemble(f), store.assemble(f))
    assert d.manifest.tiling.tile_order == "shuffled" and d.manifest.provenance["derive"]["shuffle_tiles"] == 3
    again = _derive(tmp_path, store, "--shuffle-tiles", "3", "--drop-tiles", "1:0,0", name="again")
    assert [again.keys(f.frame_id) for f in again.manifest.frames] == keys
    assert [f.sha256 for f in again.manifest.frames] == [f.sha256 for f in d.manifest.frames]


def test_late_tiles(tmp_path, store):
    d = _derive(tmp_path, store, "--drop-tiles", "2:row=1", "--late-tiles", "0:1,1@2", "--late-tiles", "1:0,0@2")
    keys = [d.keys(f.frame_id) for f in d.manifest.frames]
    assert (1, 1) not in keys[0] and (0, 0) not in keys[1]
    assert keys[2] == [(0, 0), (1, 0), (2, 0), (1, 1), (0, 0)]
    late = [h for h, _ in d.records(2)][3:]
    assert [(h.frame_id, h.flags) for h in late] == [(0, 2), (1, 2)]
    a0 = d.assemble(0)
    assert np.isnan(a0[:, 8:13, 8:16]).all()
    np.testing.assert_array_equal(d.assemble(2)[:, :8], store.assemble(2)[:, :8])
    assert all("tiles missing" in p for p in d.verify().problems)
    assert d.manifest.frames[2].valid_cells == int((~np.isnan(d.assemble(2))).sum())
    with pytest.raises(SystemExit):
        main(["derive", str(store.location), "--out", str(tmp_path / "full"), "--late-tiles", "0:0,0@3"])
    with pytest.raises(SystemExit):
        main(["derive", str(store.location), "--out", str(tmp_path / "back"), "--late-tiles", "2:0,0@1"])


def test_bad_specs_are_rejected(tmp_path, store):
    for args in (["--frames", "3:9", "--nan", "2:tile=0,0"], ["--bands", "4"], ["--nan", "0:blob=1"],
                 ["--drop-tiles", "0:5,0"], ["--nan", "x:fraction=0.1"], ["--nan", "0:fraction=2"]):
        with pytest.raises(SystemExit):
            main(["derive", str(store.location), "--out", str(tmp_path / "bad"), *args])
