import hashlib
import json
import struct

import numpy as np
import pytest
import rasterio
from rasterio.transform import from_origin

from mabench import format as fmt
from mabench import oracle
from mabench.cli import main
from mabench.sources import RasterioSource, SyntheticSource
from mabench.storage import FsspecBackend, backend_for
from mabench.store import TileStore
from mabench.tiler import tile_source
from mabench.workloads import (FocalWorkload, OutputKind, ZonalWorkload, load_workload,
                               parse_workload, workload_from_dict)


REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


def test_frame_header_layout_is_fixed():
    h = fmt.FrameHeader(7, 6, 123456789012, 70, 45, 32, 16, 3, 32633, (10.0, 0.0, 3e5, 0.0, -10.0, 5.8e6))
    b = h.pack()
    assert len(b) == 112
    v = struct.unpack("<8sIIQIIIIBBHIdII6d", b)
    assert v[:11] == (b"MPLFRM04", 7, 6, 123456789012, 70, 45, 32, 16, 3, 1, 0)
    assert v[11] == 0 and np.isnan(v[12]) and v[13:15] == (32633, 0) and v[15:] == (10.0, 0.0, 3e5, 0.0, -10.0, 5.8e6)
    u = fmt.FrameHeader.unpack_from(b)
    assert (u.tiles_x, u.tiles_y, u.maplet_nbytes) == (3, 3, 32 + 3 * 16 * 32 * 8)
    assert u.maplet_offset(2) == 112 + 2 * u.maplet_nbytes
    bad = bytearray(b); bad[48:56] = struct.pack("<d", 0.0)
    with pytest.raises(ValueError, match="noData"):
        fmt.FrameHeader.unpack_from(bytes(bad))
    with pytest.raises(ValueError, match="magic"):
        fmt.FrameHeader.unpack_from(b"MPLFRM03" + b[8:])


def test_maplet_layout_is_fixed():
    h = fmt.MapletHeader(bands=3, tile_x=-1, tile_y=2, height=4, width=5, raster_width=70, raster_height=45,
                         frame_id=9, flags=fmt.FLAG_FILLED)
    cells = np.arange(60, dtype=float).reshape(3, 4, 5)
    blob = fmt.encode_maplet(h, cells)
    assert len(blob) == h.nbytes == 32 + 60 * 8
    assert struct.unpack_from("<BBBBIiiIIII", blob) == (3, 1, 3, 1, 9, -1, 2, 4, 5, 70, 45)
    assert np.array_equal(np.frombuffer(blob, "<f8", offset=32), np.arange(60.0))
    h2, c2 = fmt.decode_maplet(blob)
    assert h2 == h and np.array_equal(c2, cells)
    with pytest.raises(ValueError, match="do not match"):
        fmt.encode_maplet(h, cells[:2])
    with pytest.raises(ValueError, match="band count"):
        fmt.MapletHeader(256, 0, 0, 1, 1, 1, 1).pack()
    with pytest.raises(ValueError, match="format"):
        fmt.decode_maplet(bytes([2]) + blob[1:])
    with pytest.raises(ValueError, match="needs"):
        fmt.decode_maplet(blob[:-8])


def test_edge_tile_valid_extent():
    h = fmt.MapletHeader(1, 2, 1, 8, 8, 20, 10)
    assert (h.valid_rows, h.valid_cols) == (2, 4)


def test_frame_file_framing_is_checked():
    fh = fmt.FrameHeader(0, 1, 0, 2, 2, 2, 2, 1)
    good = fmt.encode_frame(fh, [(fh.maplet_header(0, 0), np.ones((2, 2)))])
    assert len(list(fmt.iter_maplets(good))) == 1
    with pytest.raises(ValueError, match="trailing"):
        list(fmt.iter_maplets(good + b"x"))
    with pytest.raises(ValueError, match="truncated"):
        list(fmt.iter_maplets(good[:-1]))
    wrong = fmt.MapletHeader(1, 0, 0, 2, 2, 3, 2)
    with pytest.raises(ValueError, match="disagrees"):
        list(fmt.iter_maplets(fmt.encode_frame(fh, [(wrong, np.ones((2, 2)))])))
    outside = fh.maplet_header(1, 0)
    with pytest.raises(ValueError, match="outside grid"):
        list(fmt.iter_maplets(fmt.encode_frame(fh, [(outside, np.ones((2, 2)))])))


def test_crs_to_epsg():
    assert fmt.crs_to_epsg("EPSG:32633") == 32633
    assert fmt.crs_to_epsg(None) == 0
    assert fmt.crs_to_epsg("not a crs") == 0


@pytest.mark.parametrize("gen,params", [
    ("ramp", {}), ("checker", {"k": 5}), ("uniform", {"nodata_fraction": 0.2}),
    ("normal", {"nodata_block": [10, 10, 40, 30]}), ("waves", {}), ("constant", {"c": 3.5}),
])
def test_synthetic_roundtrip_with_partial_tiles(tmp_path, gen, params):
    src = SyntheticSource(70, 45, gen, params, bands=2, n_frames=2, seed=3)
    m = tile_source(src, backend_for(tmp_path), tile_size=16)
    assert (m.tiling.tiles_x, m.tiling.tiles_y) == (5, 3)
    store = TileStore.open(tmp_path)
    assert store.verify().ok
    yy, xx = np.mgrid[0:45, 0:70]
    for f in range(2):
        expected = np.stack([src.values(f, b, yy, xx) for b in range(2)])
        assert np.array_equal(store.assemble(f), expected, equal_nan=True)


def test_synthetic_values_do_not_depend_on_read_pattern():
    src = SyntheticSource(50, 40, "normal", {"nodata_fraction": 0.3}, seed=9)
    with next(src.frames()).open() as r:
        whole = r.read(0, 0, 40, 50)
        parts = np.concatenate([r.read(0, 0, 17, 50), r.read(17, 0, 23, 50)], axis=1)
    assert np.array_equal(whole, parts, equal_nan=True)


def test_padding_is_nodata_and_records_are_row_major(tmp_path):
    tile_source(SyntheticSource(20, 10, "constant"), backend_for(tmp_path), tile_size=8)
    store = TileStore.open(tmp_path)
    recs = list(store.records(0))
    assert [(h.tile_x, h.tile_y) for h, _ in recs] == [(x, y) for y in range(2) for x in range(3)]
    last = recs[-1][1][0]
    assert np.all(last[:2, :4] == 1.0) and np.isnan(last[2:, :]).all() and np.isnan(last[:, 4:]).all()


def test_corruption_is_detected(tmp_path):
    tile_source(SyntheticSource(32, 32, "uniform"), backend_for(tmp_path), tile_size=16)
    path = tmp_path / "frames" / "f000000.mpl"
    raw = bytearray(path.read_bytes())
    raw[100] ^= 0xFF
    path.write_bytes(bytes(raw))
    report = TileStore.open(tmp_path).verify()
    assert not report.ok and "sha256" in report.problems[0]


def _write_tif(path, data, nodata=None, scale=1.0, offset=0.0, transform=None):
    bands, h, w = data.shape
    with rasterio.open(path, "w", driver="GTiff", width=w, height=h, count=bands, dtype=data.dtype,
                       crs="EPSG:32633", transform=transform or from_origin(300000, 5800000, 10, 10),
                       nodata=nodata) as dst:
        dst.write(data)
        dst.scales = [scale] * bands
        dst.offsets = [offset] * bands


def test_geotiff_nodata_and_scale_offset(tmp_path):
    rng = np.random.default_rng(1)
    data = rng.integers(0, 1000, (2, 37, 53)).astype(np.int16)
    data[:, 5:9, 10:20] = -9999
    _write_tif(tmp_path / "a.tif", data, nodata=-9999, scale=0.5, offset=10.0)
    m = tile_source(RasterioSource([str(tmp_path / "a.tif")]), backend_for(tmp_path / "s"), 16)
    assert m.raster.crs == "EPSG:32633" and m.raster.transform[0] == 10.0 and m.raster.bands == 2
    got = TileStore.open(tmp_path / "s").assemble(0)
    expected = np.where(data == -9999, np.nan, data * 0.5 + 10.0)
    assert np.array_equal(got, expected, equal_nan=True)
    raw = tile_source(RasterioSource([str(tmp_path / "a.tif")], bands=[2], apply_scale_offset=False),
                      backend_for(tmp_path / "raw"), 16)
    assert raw.raster.bands == 1
    assert np.array_equal(TileStore.open(tmp_path / "raw").assemble(0)[0],
                          np.where(data[1] == -9999, np.nan, data[1]), equal_nan=True)


def test_multiple_files_become_frames_and_grids_must_match(tmp_path):
    for i in range(3):
        _write_tif(tmp_path / f"{i}.tif", np.full((1, 20, 20), i, np.float32))
    m = tile_source(RasterioSource([str(tmp_path / f"{i}.tif") for i in range(3)], frame_period_ms=600_000),
                    backend_for(tmp_path / "s"), 8)
    assert [f.event_time_ms for f in m.frames] == [0, 600_000, 1_200_000]
    store = TileStore.open(tmp_path / "s")
    assert [store.assemble(i)[0, 0, 0] for i in range(3)] == [0, 1, 2]
    _write_tif(tmp_path / "shifted.tif", np.zeros((1, 20, 20), np.float32),
               transform=from_origin(0, 0, 10, 10))
    with pytest.raises(ValueError, match="grid differs"):
        tile_source(RasterioSource([str(tmp_path / "0.tif"), str(tmp_path / "shifted.tif")]),
                    backend_for(tmp_path / "bad"), 8)


@pytest.mark.parametrize("shape", oracle.SHAPES)
@pytest.mark.parametrize("r", [1, 3, 6])
def test_oracle_matches_naive_definition(shape, r):
    rng = np.random.default_rng(0)
    a = rng.normal(100, 30, (31, 43))
    a[rng.random(a.shape) < 0.3] = np.nan
    a[5:15, 5:15] = np.nan
    fp = oracle.footprint(shape, r)
    for op in oracle.OPS:
        fast, slow = oracle.focal(a, op, fp), oracle.naive_focal(a, op, fp)
        assert np.array_equal(np.isnan(fast), np.isnan(slow)), (op, shape, r)
        ok = ~np.isnan(fast)
        if op in ("min", "max"):
            assert np.array_equal(fast[ok], slow[ok])
        else:
            assert np.max(np.abs(fast[ok] - slow[ok]) / np.maximum(1, np.abs(slow[ok]))) < 1e-12


def test_all_nodata_window_yields_nodata():
    a = np.full((5, 5), np.nan)
    a[0, 0] = 1.0
    for op in oracle.OPS:
        out = oracle.focal(a, op, oracle.footprint("square", 1))
        assert out[0, 0] == 1.0 and np.isnan(out[4, 4])


def test_focal_reference_is_tiled_like_input_and_independent_of_tile_size(tmp_path):
    src = SyntheticSource(61, 47, "normal", {"nodata_fraction": 0.1}, n_frames=2, seed=5)
    w = FocalWorkload("mean", "square", 20)
    results = []
    for s in (8, 32):
        tile_source(src, backend_for(tmp_path / f"in{s}"), s)
        store = TileStore.open(tmp_path / f"in{s}")
        assert any(n.level == "warning" for n in w.validate(store.manifest)) == (20 > s)
        m = w.reference(store, str(tmp_path / f"ref{s}"))
        assert m.tiling == store.manifest.tiling and m.provenance["role"] == "reference"
        ref = TileStore.open(tmp_path / f"ref{s}")
        assert ref.verify().ok
        results.append([ref.assemble(f) for f in range(2)])
    for a, b in zip(*results):
        assert np.array_equal(a, b, equal_nan=True)


def test_workload_serialisation(tmp_path):
    w = parse_workload("focal:max:circle:4")
    assert w == FocalWorkload("max", "circle", 4) and w.output_kind is OutputKind.RASTER
    w.save(tmp_path / "w.json")
    assert json.loads((tmp_path / "w.json").read_text())["params"] == {"op": "max", "shape": "circle", "radius": 4}
    assert load_workload(str(tmp_path / "w.json")) == w
    with pytest.raises(ValueError):
        FocalWorkload("median", "square", 1)


def test_zonal_is_declared_but_not_implemented(tmp_path):
    z = workload_from_dict({"family": "zonal", "params": {"zones": "zones.gpkg", "stats": ["sum"], "inclusion": "center"}})
    assert isinstance(z, ZonalWorkload) and z.output_kind is OutputKind.TABLE
    assert z.side_inputs()[0].name == "zones"
    tile_source(SyntheticSource(16, 16, "constant"), backend_for(tmp_path), 8)
    store = TileStore.open(tmp_path)
    with pytest.raises(NotImplementedError):
        z.reference(store, str(tmp_path / "out"))
    with pytest.raises(NotImplementedError):
        z.prepare_side_inputs(store.manifest, str(tmp_path / "zones"))


def test_object_store_backend_roundtrip():
    loc = "memory://bucket/store1"
    assert isinstance(backend_for(loc), FsspecBackend)
    src = SyntheticSource(40, 30, "uniform", {"nodata_fraction": 0.1}, seed=2)
    tile_source(src, backend_for(loc), 16)
    store = TileStore.open(loc)
    assert store.verify().ok
    yy, xx = np.mgrid[0:30, 0:40]
    assert np.array_equal(store.assemble(0)[0], src.values(0, 0, yy, xx), equal_nan=True)


def test_cli_end_to_end(tmp_path, capsys):
    s, r, w = str(tmp_path / "s"), str(tmp_path / "r"), str(tmp_path / "w.json")
    assert main(["synth", "--out", s, "--width", "50", "--height", "30", "--generator", "waves",
                 "--param", "nodata_block=0,0,8,8", "--frames", "2", "--tile-size", "16"]) == 0
    assert main(["verify", s]) == 0
    assert main(["workload", "focal:min:square:2", "-o", w]) == 0
    assert main(["reference", s, "--workload", w, "--out", r]) == 0
    assert main(["inspect", r]) == 0
    assert "50 x 30" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        main(["synth", "--out", s, "--width", "8", "--height", "8", "--generator", "constant", "--tile-size", "8"])
    (tmp_path / "s" / "frames" / "f000001.mpl").write_bytes(b"garbage")
    assert main(["verify", s]) == 1


def test_prepare_expands_and_sorts_glob_patterns(tmp_path):
    for name in ("b_1200.tif", "a_1100.tif", "c_1300.tif"):
        _write_tif(tmp_path / name, np.full((1, 8, 8), float(name[2:6]), np.float32))
    assert main(["prepare", str(tmp_path / "*.tif"), "--out", str(tmp_path / "s"), "--tile-size", "8"]) == 0
    store = TileStore.open(tmp_path / "s")
    assert [store.assemble(f)[0, 0, 0] for f in range(3)] == [1100.0, 1200.0, 1300.0]
    with pytest.raises(SystemExit, match="no files match"):
        main(["prepare", str(tmp_path / "*.nope"), "--out", str(tmp_path / "x"), "--tile-size", "8"])


def _write_multivar_netcdf(path):
    from scipy.io import netcdf_file
    f = netcdf_file(str(path), "w")
    f.createDimension("y", 20)
    f.createDimension("x", 30)
    f.createVariable("y", "f8", ("y",))[:] = np.arange(20)[::-1] * 1000.0
    f.createVariable("x", "f8", ("x",))[:] = np.arange(30) * 1000.0
    for i, name in enumerate(("CMI", "DQF")):
        f.createVariable(name, "f4", ("y", "x"))[:] = np.full((20, 30), i + 1, "f4")
    f.close()


def test_netcdf_container_needs_a_variable(tmp_path):
    nc = tmp_path / "frame.nc"
    _write_multivar_netcdf(nc)
    with pytest.raises(ValueError, match="CMI, DQF.*--variable"):
        main(["prepare", str(nc), "--out", str(tmp_path / "s0"), "--tile-size", "16"])
    assert main(["prepare", str(nc), "--variable", "DQF", "--out", str(tmp_path / "s"), "--tile-size", "16"]) == 0
    store = TileStore.open(tmp_path / "s")
    assert store.manifest.raster.bands == 1 and np.all(store.assemble(0) == 2.0)
    with pytest.raises(SystemExit, match="available: CMI, DQF"):
        main(["prepare", str(nc), "--variable", "XYZ", "--out", str(tmp_path / "s2"), "--tile-size", "16"])
    _write_tif(tmp_path / "plain.tif", np.zeros((1, 8, 8), np.float32))
    with pytest.raises(SystemExit, match="plain raster"):
        main(["prepare", str(tmp_path / "plain.tif"), "--variable", "CMI", "--out", str(tmp_path / "s3"), "--tile-size", "8"])
