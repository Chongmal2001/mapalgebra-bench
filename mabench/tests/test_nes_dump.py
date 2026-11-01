"""import-nes: synthetic NES Binary dumps built from oracle outputs, with injected faults (bench-capabilities 9.1)."""

import json
import random
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mabench import format as fmt
from mabench.cli import main
from mabench.nes_dump import DUMP_HEADER_LINE, DUMP_RECORD, import_nes, index_dump
from mabench.store import TileStore
from mabench.workloads import parse_workload

WORKLOAD = "focal:mean:circle:2"
REFS = [("tiny-1b", "sum-square-1"), ("tiny-1b", "mean-circle-2"), ("tiny-2b", "sum-square-1"),
        ("tiny-2b", "mean-circle-2"), ("r-gt-t", "sum-square-5")]


@pytest.fixture(scope="session")
def stores(tmp_path_factory):
    """Small stores and their oracle outputs, generated once per test session (nothing committed)."""
    root = tmp_path_factory.mktemp("stores")
    synth = {"tiny-1b": ["--width", "8", "--height", "8", "--generator", "ramp", "--frames", "3"],
             "tiny-2b": ["--width", "10", "--height", "6", "--generator", "normal", "--bands", "2", "--frames", "2",
                         "--seed", "5", "--param", "nodata_block=0,0,2,2", "--param", "nodata_fraction=0.1"],
             "r-gt-t": ["--width", "12", "--height", "12", "--generator", "waves", "--frames", "2"]}
    for name, args in synth.items():
        assert main(["synth", "--out", str(root / name), *args, "--frame-period-ms", "100", "--tile-size", "4"]) == 0
    for store, wl in REFS:
        assert main(["reference", str(root / store), "--workload", "focal:" + wl.replace("-", ":"),
                     "--out", str(root / "ref" / store / wl)]) == 0
    return root


def _records(ref: TileStore, src_id=1):
    """(srcId, srcFrameId, ts, maplet bytes) per tile, as NES would emit them: maplet frame_id = output frame number
    from 1 (here input frame id + 1), in an interleaved, shuffled order."""
    out = []
    for info in ref.manifest.frames:
        for h, cells in ref.records(info.frame_id):
            out.append([src_id, info.frame_id, info.event_time_ms,
                        fmt.encode_maplet(replace(h, frame_id=info.frame_id + 1), cells)])
    random.Random(7).shuffle(out)
    return out


def _write_dump(path, records, header=DUMP_HEADER_LINE):
    with open(path, "wb") as fh:
        fh.write(header)
        for src_id, fid, ts, maplet in records:
            fh.write(DUMP_RECORD.pack(src_id, fid, ts, len(maplet)) + maplet)
    return str(path)


def _patch(maplet: bytes, **fields) -> bytes:
    h = fmt.MapletHeader.unpack_from(maplet)
    return replace(h, **fields).pack() + maplet[fmt.MAPLET_PREAMBLE.size:]


@pytest.fixture()
def env(tmp_path, stores):
    inp, ref = TileStore.open(stores / "tiny-2b"), TileStore.open(stores / "ref" / "tiny-2b" / "mean-circle-2")
    return tmp_path, inp, ref, parse_workload(WORKLOAD), _records(ref)


def test_clean_dump_reproduces_the_reference_byte_for_byte(env):
    tmp, inp, ref, w, recs = env
    m, rep = import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "out"))
    out = TileStore.open(tmp / "out")
    assert out.verify().ok and not rep.notes()
    # same frame header, maplets in row-major order with frame_id 0 / flags 0, same cells -> same file bytes
    assert [f.sha256 for f in m.frames] == [f.sha256 for f in ref.manifest.frames]
    assert m.provenance["role"] == "nes" and m.provenance["workload"] == w.to_dict()
    assert m.provenance["nes"]["out_frame_of"] == {0: 1, 1: 2}
    rep = w.compare(inp, ref, out)
    assert rep.passed and rep.totals()["all"].summary()["exact_rate"] == 1.0


def test_missing_tile_is_written_as_partial_frame_and_reported_by_compare(env):
    tmp, inp, ref, w, recs = env
    lost = next(r for r in recs if r[1] == 1)
    h = fmt.MapletHeader.unpack_from(lost[3])
    recs.remove(lost)
    m, rep = import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "out"))
    assert rep.missing_tiles == {1: [[h.tile_x, h.tile_y]]}
    assert m.frame(1).n_records == 5 and m.frame(0).n_records == 6
    out = TileStore.open(tmp / "out")
    assert any("1 tiles missing" in p for p in out.verify().problems)
    cr = w.compare(inp, ref, out)
    assert not cr.passed and any("missing from candidate" in p for p in cr.coverage_problems)


def test_identical_duplicate_dropped_differing_duplicate_rejected(env):
    tmp, inp, ref, w, recs = env
    _, rep = import_nes(_write_dump(tmp / "d.bin", recs + [list(recs[3])]), inp, w, str(tmp / "out"))
    assert rep.duplicates_dropped == 1
    assert w.compare(inp, ref, TileStore.open(tmp / "out")).passed
    bad = list(recs[3])
    cells = bytearray(bad[3])
    cells[-8:] = np.float64(12345.0).tobytes()
    bad[3] = bytes(cells)
    with pytest.raises(ValueError, match="appears twice with different contents"):
        import_nes(_write_dump(tmp / "d2.bin", recs + [bad]), inp, w, str(tmp / "out2"))


def test_filled_flag_goes_to_provenance_not_into_the_file(env):
    tmp, inp, ref, w, recs = env
    r = next(r for r in recs if r[1] == 0)
    h = fmt.MapletHeader.unpack_from(r[3])
    r[3] = _patch(r[3], flags=fmt.FLAG_FILLED)
    m, rep = import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "out"))
    assert rep.filled_tiles == {0: [[h.tile_x, h.tile_y]]}
    assert m.provenance["nes"]["filled_tiles"] == {0: [[h.tile_x, h.tile_y]]}
    assert TileStore.open(tmp / "out").verify().ok          # flags 0 in the file


def test_framing_errors(env):
    tmp, inp, _, w, recs = env
    with pytest.raises(ValueError, match="expected the File sink schema line"):
        import_nes(_write_dump(tmp / "h.bin", recs, header=b"srcId:UINT32:NOT_NULLABLE\n"), inp, w, str(tmp / "o1"))
    path = _write_dump(tmp / "t.bin", recs)
    data = Path(path).read_bytes()
    Path(path).write_bytes(data[:-10])
    last = len(DUMP_HEADER_LINE) + (len(recs) - 1) * (DUMP_RECORD.size + len(recs[0][3]))
    with pytest.raises(ValueError, match=f"truncated record at offset {last}"):
        index_dump(path)
    Path(path).write_bytes(data + b"\x01\x02")
    with pytest.raises(ValueError, match="truncated record head"):
        index_dump(path)
    with pytest.raises(ValueError, match="maplets are"):
        index_dump(_write_dump(tmp / "l.bin", recs), maplet_nbytes=len(recs[0][3]) - 8)


@pytest.mark.parametrize("fault, message", [
    (lambda recs: recs[0].__setitem__(2, recs[0][2] + 1), "records disagree"),
    (lambda recs: [r.__setitem__(2, r[2] + 5) for r in recs if r[1] == 1], r"ts 105 != event time 100"),
    (lambda recs: recs[0].__setitem__(1, 9), "srcFrameId 9 is not a frame"),
    (lambda recs: recs[0].__setitem__(3, _patch(recs[0][3], frame_id=0)), "frame_id 0"),
    (lambda recs: recs[0].__setitem__(3, _patch(recs[0][3], tile_x=7)), "outside grid"),
    (lambda recs: recs[0].__setitem__(0, 2), "several sources"),
])
def test_consistency_errors(env, fault, message):
    tmp, inp, _, w, recs = env
    fault(recs)
    with pytest.raises(ValueError, match=message):
        import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "out"))


def test_src_id_filter_and_frames_without_output(env):
    tmp, inp, ref, w, recs = env
    recs = [r for r in recs if r[1] == 0]
    for r in recs:
        r[0] = 4
    with pytest.raises(ValueError, match="expected 1"):
        import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "o"), src_id=1)
    m, rep = import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "o2"), src_id=4)
    assert rep.frames_without_output == [1] and [f.frame_id for f in m.frames] == [0]
    cr = w.compare(inp, ref, TileStore.open(tmp / "o2"))
    assert not cr.passed and "frame 1 missing from candidate" in cr.coverage_problems


def test_padding_that_is_not_nodata_is_kept_and_reported(env):
    tmp, inp, ref, w, recs = env
    r = next(r for r in recs if fmt.MapletHeader.unpack_from(r[3]).tile_x == 2)   # 10 px wide, 4 px tiles: padded
    cells = bytearray(r[3])
    cells[-8:] = np.float64(0.0).tobytes()                    # last cell of the last band lies in the padding
    r[3] = bytes(cells)
    _, rep = import_nes(_write_dump(tmp / "d.bin", recs), inp, w, str(tmp / "out"))
    assert sum(len(v) for v in rep.padding_not_nodata.values()) == 1
    assert w.compare(inp, ref, TileStore.open(tmp / "out")).passed          # compare ignores padding


def test_cli_import_then_compare(env, stores, capsys):
    tmp, _, _, _, recs = env
    INPUT, REF = stores / "tiny-2b", stores / "ref" / "tiny-2b" / "mean-circle-2"
    dump = _write_dump(tmp / "d.bin", recs)
    assert main(["import-nes", dump, "--input", str(INPUT), "--workload", WORKLOAD, "--out", str(tmp / "out")]) == 0
    with pytest.raises(SystemExit, match="already holds a tile store"):
        main(["import-nes", dump, "--input", str(INPUT), "--workload", WORKLOAD, "--out", str(tmp / "out")])
    assert main(["compare", str(tmp / "out"), "--reference", str(REF), "--input", str(INPUT)]) == 0
    assert "PASS" in capsys.readouterr().out
    prov = json.loads((tmp / "out" / "manifest.json").read_text())["provenance"]
    assert prov["tool"] == "mabench" and prov["nes"]["records"] == len(recs)
    with pytest.raises(SystemExit, match="import-nes: .*schema line"):
        bad = tmp / "bad.bin"
        bad.write_bytes(b"x\n")
        main(["import-nes", str(bad), "--input", str(INPUT), "--workload", WORKLOAD, "--out", str(tmp / "o3")])
    # a workload that disagrees with the reference is refused by compare
    main(["import-nes", dump, "--input", str(INPUT), "--workload", "focal:sum:square:1", "--out", str(tmp / "o4")])
    with pytest.raises(ValueError, match="was produced by workload"):
        main(["compare", str(tmp / "o4"), "--reference", str(REF), "--input", str(INPUT)])


@pytest.mark.parametrize("store, wl", REFS)
def test_round_trip_on_every_reference(tmp_path, stores, store, wl):
    inp, r = TileStore.open(stores / store), TileStore.open(stores / "ref" / store / wl)
    w = parse_workload("focal:" + wl.replace("-", ":"))
    m, _ = import_nes(_write_dump(tmp_path / "d.bin", _records(r)), inp, w, str(tmp_path / "out"))
    assert [f.sha256 for f in m.frames] == [f.sha256 for f in r.manifest.frames]
    assert w.compare(inp, r, TileStore.open(tmp_path / "out")).passed


def test_nes_query_carries_geometry_window_and_dump_schema(stores):
    from mabench.nes_dump import nes_query
    INPUT = stores / "tiny-2b"
    q = nes_query(TileStore.open(INPUT), parse_workload("focal:min:circle:2"), "/tmp/d.bin")
    assert "FOCAL_MIN(raster, DISC, 2, NODATA, nan, 10, 6, 4, 4)" in q
    assert "TUMBLING(ts, size 100 ms)" in q and "'Binary' AS \"SINK\".OUTPUT_FORMAT" in q
    assert "dump(srcId UINT32 NOT NULL, srcFrameId UINT32 NOT NULL, ts UINT64 NOT NULL, raster VARSIZED NOT NULL)" in q
    assert f"'{INPUT}' AS \"SOURCE\".FILE_PATH" in q


def test_schema_line_is_matched_case_insensitively(env):
    tmp, inp, ref, w, recs = env           # NES upper-cases unquoted identifiers when it prints the sink schema
    _, rep = import_nes(_write_dump(tmp / "d.bin", recs, header=DUMP_HEADER_LINE.upper()), inp, w, str(tmp / "out"))
    assert w.compare(inp, ref, TileStore.open(tmp / "out")).passed
    with pytest.raises(ValueError, match="schema line"):    # order still matters
        bad = b"SRCFRAMEID:UINT32:NOT_NULLABLE,SRCID:UINT32:NOT_NULLABLE,TS:UINT64:NOT_NULLABLE,RASTER:VARSIZED:NOT_NULLABLE\n"
        import_nes(_write_dump(tmp / "e.bin", recs, header=bad), inp, w, str(tmp / "o2"))


def test_nes_query_yaml_is_a_nes_cli_topology(stores):
    yaml = pytest.importorskip("yaml")
    from mabench.nes_dump import nes_query
    t = yaml.safe_load(nes_query(TileStore.open(stores / "tiny-2b"), parse_workload("focal:sum:square:1"), "/x/d.bin", yaml=True,
                                 store_path="/data/tiny-2b"))
    assert set(t) == {"query", "sinks", "logical", "physical", "workers"}
    assert "FOCAL_SUM(raster, SQUARE, 1, NODATA, nan, 10, 6, 4, 4)" in t["query"]
    assert t["sinks"][0]["config"] == {"file_path": "/x/d.bin", "output_format": "Binary"}
    assert [f["name"] for f in t["sinks"][0]["schema"]] == ["srcId", "srcFrameId", "ts", "raster"]
    assert t["physical"][0]["type"] == "RasterFile" and t["physical"][0]["parser_config"] == {"type": "NATIVE"}
    assert t["physical"][0]["source_config"] == {"file_path": "/data/tiny-2b", "src_id": 1}


def test_nes_query_void_sink(stores):
    yaml = pytest.importorskip("yaml")
    from mabench.nes_dump import nes_query
    t = yaml.safe_load(nes_query(TileStore.open(stores / "tiny-2b"), parse_workload("focal:sum:square:1"), None, yaml=True))
    assert t["sinks"][0]["type"] == "Void" and t["sinks"][0]["config"] == {}
    with pytest.raises(ValueError):
        nes_query(TileStore.open(stores / "tiny-2b"), parse_workload("focal:sum:square:1"), None)


def test_nes_query_frames_per_window(stores):
    from mabench.nes_dump import nes_query
    q = nes_query(TileStore.open(stores / "tiny-1b"), parse_workload("focal:sum:square:1"), "/x/d.bin", frames_per_window=3)
    assert "WINDOW TUMBLING(ts, size 300 ms)" in q


def test_sliding_windows_multiplicity(env):
    """Each frame emitted in 2 windows under different output frame numbers: one copy kept, multiplicity checked."""
    tmp, inp, ref, w, recs = env
    second = [[s, f, ts, _patch(m, frame_id=fmt.MapletHeader.unpack_from(m).frame_id + 10)] for s, f, ts, m in recs]
    m, rep = import_nes(_write_dump(tmp / "d.bin", recs + second), inp, w, str(tmp / "out"), windows_per_frame=2)
    assert rep.out_frames_of == {0: [1, 11], 1: [2, 12]} and not rep.multiplicity_problems
    assert rep.multiplicity == {0: {2: 6}, 1: {2: 6}}
    assert [f.sha256 for f in m.frames] == [f.sha256 for f in ref.manifest.frames]
    assert w.compare(inp, ref, TileStore.open(tmp / "out")).passed
    # one tile emitted only once: reported, and compare fails
    short = second[1:]
    _, rep = import_nes(_write_dump(tmp / "d2.bin", recs + short), inp, w, str(tmp / "out2"), windows_per_frame=2)
    assert len(rep.multiplicity_problems) == 1 and "emitted 1x, expected 2x" in rep.multiplicity_problems[0]
    cr = w.compare(inp, ref, TileStore.open(tmp / "out2"))
    assert not cr.passed and any("multiplicity" in p for p in cr.coverage_problems)


def test_nes_query_sliding_clause(stores):
    from mabench.nes_dump import nes_query
    q = nes_query(TileStore.open(stores / "tiny-1b"), parse_workload(WORKLOAD), "/d.bin", frames_per_window=2,
                  slide_frames=1)
    assert "WINDOW SLIDING(ts, size 200 ms, advance by 100 ms)" in q
    assert "TUMBLING(ts, size 200 ms)" in nes_query(TileStore.open(stores / "tiny-1b"), parse_workload(WORKLOAD),
                                                   "/d.bin", frames_per_window=2, slide_frames=2)
    with pytest.raises(ValueError):
        nes_query(TileStore.open(stores / "tiny-1b"), parse_workload(WORKLOAD), "/d.bin", frames_per_window=3,
                  slide_frames=2)


def test_nes_query_bands_subquery(stores):
    from mabench.nes_dump import nes_query
    q = nes_query(TileStore.open(stores / "tiny-2b"), parse_workload(WORKLOAD), "/d.bin", bands=[2, 1])
    assert "FROM (SELECT ts, srcId, srcFrameId, BANDS(raster, 1, 0) AS raster FROM tiles) WINDOW TUMBLING" in q
    with pytest.raises(ValueError):
        nes_query(TileStore.open(stores / "tiny-2b"), parse_workload(WORKLOAD), "/d.bin", bands=[3])
