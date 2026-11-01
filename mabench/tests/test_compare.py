import json

import numpy as np
import pytest

from mabench import oracle
from mabench.cli import main
from mabench.compare import _near, compare_focal, strata_masks, ulp_distance
from mabench.sources import ArraySource, SyntheticSource
from mabench.storage import backend_for
from mabench.store import TileStore
from mabench.tiler import tile_source
from mabench.workloads import FocalWorkload


def _store_from_arrays(path, like: TileStore, frames, workload=None, skip_tiles=()):
    """Write arrays (one (bands, H, W) per frame) as a tile store with the tiling of `like`."""
    m = like.manifest
    src = ArraySource(m.raster, [(f.frame_id, f.event_time_ms, a) for f, a in zip(m.frames, frames)])
    out = tile_source(src, backend_for(path), m.tiling.tile_size,
                      provenance={"role": "candidate", **({"workload": workload.to_dict()} if workload else {})})
    if skip_tiles:   # drop records to simulate a system that lost tiles
        from mabench import format as fmt
        for info in out.frames:
            buf = bytes(TileStore.open(path).backend.read_buffer(info.key))
            kept = [(h, c.copy()) for h, c in fmt.iter_maplets(buf) if (h.tile_x, h.tile_y) not in skip_tiles]
            data = fmt.encode_frame(fmt.unpack_frame_header(buf), kept)
            (path / info.key).write_bytes(data)
    return TileStore.open(path)


@pytest.fixture()
def setup(tmp_path):
    src = SyntheticSource(70, 50, "normal", {"nodata_fraction": 0.1, "nodata_block": [20, 20, 30, 30]},
                          bands=2, n_frames=2, seed=11)
    tile_source(src, backend_for(tmp_path / "in"), 16)
    inp = TileStore.open(tmp_path / "in")
    w = FocalWorkload("mean", "square", 2)
    w.reference(inp, str(tmp_path / "ref"))
    ref = TileStore.open(tmp_path / "ref")
    return tmp_path, inp, ref, w


def test_identical_candidate_passes(setup):
    tmp, inp, ref, w = setup
    cand = _store_from_arrays(tmp / "cand", inp, [ref.assemble(f.frame_id) for f in ref.manifest.frames], w)
    rep = w.compare(inp, ref, cand)
    t = rep.totals()["all"].summary()
    assert rep.passed and t["exact_rate"] == 1.0 and t["nodata_agreement"] == 1.0
    assert rep.totals()["all"].cells == 2 * 2 * 70 * 50


def test_tolerance_is_scaled_by_neighbourhood_magnitude(setup):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    tiny = [a * (1 + 1e-15) for a in frames]                       # last-ULP drift, like sliding sums
    assert w.compare(inp, ref, _store_from_arrays(tmp / "tiny", inp, tiny, w)).passed
    big = [a + 1e-6 for a in frames]
    rep = w.compare(inp, ref, _store_from_arrays(tmp / "big", inp, big, w))
    assert not rep.passed and rep.totals()["all"].within_tolerance == 0


def test_min_max_require_bitwise_equality(tmp_path, setup):
    tmp, inp, _, _ = setup
    w = FocalWorkload("max", "square", 1)
    w.reference(inp, str(tmp / "refmax"))
    ref = TileStore.open(tmp / "refmax")
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    frames[0][0, 3, 3] = np.nextafter(frames[0][0, 3, 3], np.inf)
    rep = w.compare(inp, ref, _store_from_arrays(tmp / "c", inp, frames, w))
    assert not rep.passed and rep.totals()["all"].max_ulp == 1.0


def test_nodata_mismatch_and_missing_tiles_fail(setup):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    frames[1][1, 40, 60] = np.nan
    rep = w.compare(inp, ref, _store_from_arrays(tmp / "nd", inp, frames, w))
    assert not rep.passed and rep.totals()["all"].nodata_only_candidate == 1
    rep = w.compare(inp, ref, _store_from_arrays(tmp / "miss", inp, [ref.assemble(f.frame_id) for f in ref.manifest.frames],
                                                 w, skip_tiles={(1, 1)}))
    assert not rep.passed and any("missing from candidate" in p for p in rep.coverage_problems)


def test_errors_are_attributed_to_strata(setup):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    frames[0][0, 15, 40] += 1.0      # row 15 is next to the tile edge at 16: seam band (r = 2)
    rep = w.compare(inp, ref, _store_from_arrays(tmp / "seam", inp, frames, w))
    tot = rep.totals()
    seam_bad = tot["seam"].data_both - tot["seam"].within_tolerance
    assert seam_bad == 1 and tot["interior"].data_both == tot["interior"].within_tolerance


def test_strata_masks():
    x = np.zeros((1, 40, 40))
    x[0, 5, 5] = np.nan
    m = strata_masks(x, 16, 2, {(2, 2)}, oracle.footprint("square", 2))
    assert m["seam"][20, 14] and m["seam"][20, 17] and not m["seam"][20, 8] and not m["seam"][20, 39]
    assert m["raster_edge"][0, 20] and m["raster_edge"][39, 20] and not m["raster_edge"][20, 20]
    assert m["near_nodata"][7, 7] and not m["near_nodata"][8, 8]
    assert m["near_missing_tile"][30, 30] and m["near_missing_tile"][30, 31] and not m["near_missing_tile"][28, 28]
    assert not (m["interior"] & (m["seam"] | m["raster_edge"] | m["near_nodata"] | m["near_missing_tile"])).any()


def test_ulp_distance():
    one = np.array([1.0, 0.0, -1.0, 2.0])
    nxt = np.array([np.nextafter(1.0, 2.0), -0.0, np.nextafter(-1.0, -2.0), 2.0])
    assert list(ulp_distance(one, nxt)) == [1.0, 0.0, 1.0, 0.0]


def _one_tile_halo_model(frame, op, shape, r, s):
    """Model of the hypothesised GeoTrellis behaviour: each tile only sees its 8 neighbour tiles."""
    _, H, W = frame.shape
    out = np.full_like(frame, np.nan)
    fp = oracle.footprint(shape, r)
    for ty in range(0, H, s):
        for tx in range(0, W, s):
            y0, x0 = max(0, ty - s), max(0, tx - s)
            y1, x1 = min(H, ty + 2 * s), min(W, tx + 2 * s)
            for b in range(frame.shape[0]):
                win = oracle.focal(frame[b, y0:y1, x0:x1], op, fp)
                out[b, ty:ty + s, tx:tx + s] = win[ty - y0:ty - y0 + s, tx - x0:tx - x0 + s][:min(s, H - ty), :min(s, W - tx)]
    return out


@pytest.mark.parametrize("r,should_pass", [(3, True), (16, True), (20, False)])
def test_one_tile_halo_is_exact_iff_radius_fits_in_a_tile(setup, r, should_pass):
    """Locality argument (spec §3.2): a halo of one tile is enough exactly when r <= tile size."""
    tmp, inp, _, _ = setup
    w = FocalWorkload("max", "square", r)
    w.reference(inp, str(tmp / f"ref{r}"))
    ref = TileStore.open(tmp / f"ref{r}")
    frames = [_one_tile_halo_model(inp.assemble(f.frame_id), "max", "square", r, 16) for f in inp.manifest.frames]
    rep = w.compare(inp, ref, _store_from_arrays(tmp / f"halo{r}", inp, frames, w))
    assert rep.passed == should_pass
    if not should_pass:
        assert rep.totals()["interior"].data_both == rep.totals()["interior"].exact   # errors only where halos reach far


def test_cli_compare(setup, tmp_path, capsys):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    _store_from_arrays(tmp / "ok", inp, frames, w)
    frames[0][1, 10, 10] += 5.0
    _store_from_arrays(tmp / "bad", inp, frames, w)
    assert main(["compare", str(tmp / "ok"), "--reference", str(tmp / "ref"), "--input", str(tmp / "in"),
                 "--json", str(tmp / "r.json")]) == 0
    assert json.loads((tmp / "r.json").read_text())["passed"] is True
    assert main(["compare", str(tmp / "bad"), "--reference", str(tmp / "ref"), "--input", str(tmp / "in"),
                 "--heatmap", str(tmp / "h.png")]) == 1
    assert (tmp / "h.png").stat().st_size > 1000
    out = capsys.readouterr().out
    assert "FAIL" in out and "seam" in out
    with pytest.raises(ValueError, match="workload"):
        main(["compare", str(tmp / "ok"), "--reference", str(tmp / "ref"), "--input", str(tmp / "in"),
              "--workload", "focal:max:square:2"])


# --------------------------------------------------------------------------- strip-wise processing


def _nodata_store(tmp, tile=16, bands=2):
    src = SyntheticSource(53, 70, "normal", {"nodata_fraction": 0.05, "nodata_block": [20, 3, 41, 25]},
                          bands=bands, n_frames=2, seed=7)
    tile_source(src, backend_for(tmp / "in"), tile)
    return TileStore.open(tmp / "in")


def test_assemble_rows_equals_slices_of_assemble(tmp_path):
    store = _nodata_store(tmp_path)
    full = store.assemble(1)
    for a, b in [(0, 1), (0, 70), (5, 21), (15, 17), (31, 32), (48, 70)]:
        assert np.array_equal(store.assemble_rows(1, a, b), full[:, a:b], equal_nan=True)
    with pytest.raises(ValueError):
        store.assemble_rows(1, 10, 71)


@pytest.mark.parametrize("op,shape,r", [("mean", "square", 3), ("sum", "circle", 5), ("max", "square", 20),
                                         ("min", "circle", 2)])
def test_strip_reference_is_bit_identical_to_whole_frame_oracle(tmp_path, op, shape, r):
    """The reference is computed strip by strip with an r-row halo; r = 20 > tile size 16 spans several strips."""
    store = _nodata_store(tmp_path)
    w = FocalWorkload(op, shape, r)
    w.reference(store, str(tmp_path / "ref"))
    ref = TileStore.open(tmp_path / "ref")
    fp = oracle.footprint(shape, r)
    for f in store.manifest.frames:
        x = store.assemble(f.frame_id)
        whole = np.stack([oracle.focal(x[b], op, fp) for b in range(x.shape[0])])
        got = ref.assemble(f.frame_id)
        assert np.array_equal(got.view(np.int64), whole.view(np.int64))  # bitwise, NaN payloads included


def test_strata_masks_strip_equals_whole_frame():
    rng = np.random.default_rng(1)
    x = rng.normal(size=(2, 61, 45))
    x[0, rng.random((61, 45)) < 0.03] = np.nan
    missing, fp, r, s = {(1, 1), (2, 3)}, oracle.footprint("circle", 4), 4, 16
    whole = strata_masks(x, s, r, missing, fp)
    for y0 in range(0, 61, s):
        y1 = min(61, y0 + s)
        a0, a1 = max(0, y0 - r), min(61, y1 + r)
        part = strata_masks(x[:, a0:a1], s, r, missing, fp, row0=a0, height=61)
        for name in whole:
            assert np.array_equal(part[name][y0 - a0:y0 - a0 + (y1 - y0)], whole[name][y0:y1]), (name, y0)


def test_heatmap_blocks_keep_the_maximum(tmp_path):
    from mabench.compare import _Heatmap
    hm = _Heatmap(10, 7, max_px=3)                              # block size 4
    ref = np.zeros((1, 10, 7))
    cand = ref.copy()
    cand[0, 9, 6] = 5.0
    cand[0, 1, 1] = 2.0
    cand[0, 2, 2] = np.nan
    hm.add(0, cand[:, :6], ref[:, :6])
    hm.add(6, cand[:, 6:], ref[:, 6:])
    assert hm.err.shape == (3, 2) and hm.err[0, 0] == 2.0 and hm.err[2, 1] == 5.0 and hm.err.sum() == 7.0
    assert hm.mismatch[0, 0] and hm.mismatch.sum() == 1
    hm.write(str(tmp_path / "h.png"), 4, "test")
    assert (tmp_path / "h.png").stat().st_size > 0


@pytest.mark.parametrize("shape", ["square", "circle"])
def test_near_equals_correlation(shape):
    from scipy import ndimage as ndi
    rng = np.random.default_rng(5)
    for r in (0, 1, 2, 5, 9):
        fp = oracle.footprint(shape, r)
        for density in (0.0, 0.002, 0.05):
            m = rng.random((37, 29)) < density
            want = ndi.correlate(m.astype(float), fp.astype(float), mode="constant", cval=0.0) > 0.5
            assert np.array_equal(_near(m, fp), want), (r, density)


def test_parallel_compare_equals_serial(setup):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    frames[1][0, 30, 40] += 1.0
    cand = _store_from_arrays(tmp / "cand", inp, frames, w, skip_tiles={(2, 1)})
    serial = compare_focal(inp, ref, cand, "mean", "square", 2, jobs=1, heatmap=str(tmp / "s.png"))
    par = compare_focal(inp, ref, cand, "mean", "square", 2, jobs=2, heatmap=str(tmp / "p.png"))
    assert json.dumps(serial.to_dict()) == json.dumps(par.to_dict())
    assert not par.passed and (tmp / "p.png").stat().st_size > 1000


def test_tolerance_utilisation(setup):
    tmp, inp, ref, w = setup
    frames = [ref.assemble(f.frame_id) for f in ref.manifest.frames]
    same = w.compare(inp, ref, _store_from_arrays(tmp / "same", inp, frames, w)).totals()["all"]
    assert same.max_tol_util == 0.0
    tiny = w.compare(inp, ref, _store_from_arrays(tmp / "tiny", inp, [a * (1 + 1e-15) for a in frames], w))
    assert 0.0 < tiny.totals()["all"].max_tol_util <= 1.0
    big = w.compare(inp, ref, _store_from_arrays(tmp / "big", inp, [a + 1e-6 for a in frames], w))
    assert big.totals()["all"].max_tol_util > 1.0
    assert "max_tol_util" in big.to_dict()["totals"]["all"]


def test_tiles_missing_from_the_input_are_not_compared(tmp_path):
    src = tmp_path / "src"
    assert main(["synth", "--out", str(src), "--width", "40", "--height", "30", "--generator", "normal",
                 "--frames", "2", "--tile-size", "10", "--seed", "3"]) == 0
    assert main(["derive", str(src), "--out", str(tmp_path / "gap"), "--drop-tiles", "*:1,1"]) == 0
    inp = TileStore.open(tmp_path / "gap")
    w = FocalWorkload("sum", "square", 2)
    w.reference(inp, str(tmp_path / "ref"))
    ref = TileStore.open(tmp_path / "ref")
    # a streaming candidate: everything but the tile that never arrived
    cand = _store_from_arrays(tmp_path / "cand", inp, [ref.assemble(f.frame_id) for f in ref.manifest.frames], w,
                              skip_tiles={(1, 1)})
    rep = w.compare(inp, ref, cand)
    assert rep.passed, rep.table()
    assert rep.missing_input_tiles == {0: 1, 1: 1}
    t = rep.totals()
    assert t["all"].cells == 2 * (40 * 30 - 100)                    # the missing tile's cells are left out
    assert t["near_missing_tile"].cells > 0 and t["near_missing_tile"].nodata_only_candidate == 0
    # NES emits the missing positions too (FILLED, from the neighbours): then they are compared
    full = _store_from_arrays(tmp_path / "full", inp, [ref.assemble(f.frame_id) for f in ref.manifest.frames], w)
    rep = w.compare(inp, ref, full)
    assert rep.passed and not rep.missing_input_tiles and rep.totals()["all"].cells == 2 * 40 * 30


def test_multiplicity_problems_fail_the_comparison(setup):
    tmp, inp, ref, w = setup
    cand = _store_from_arrays(tmp / "cand", inp, [ref.assemble(f.frame_id) for f in ref.manifest.frames], w)
    m = cand.manifest
    m.provenance["nes"] = {"multiplicity_problems": ["frame 0: 3 tile(s) emitted 1x, expected 2x"]}
    m.save(cand.backend)
    rep = w.compare(inp, ref, TileStore.open(tmp / "cand"))
    assert not rep.passed and any("multiplicity" in p for p in rep.coverage_problems)
