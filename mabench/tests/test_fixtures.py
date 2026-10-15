"""The GeoTrellis test fixtures are mabench output committed to the Scala side of the repository.

This test regenerates them and requires every frame file to be byte-identical (same SHA-256) to the
committed copy, so the Python and Scala halves cannot silently drift apart. If it fails after a deliberate
format change, run scripts/regen-fixtures.sh.
"""

import json
import pathlib

import pytest

from mabench.cli import main

REPO = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = REPO / "geotrellis-baseline" / "src" / "test" / "resources" / "fixtures"
WORKLOADS = ["mean:square:2", "sum:square:2", "min:square:2", "max:square:2",
             "mean:circle:3", "max:circle:3", "mean:square:20"]


def _frame_hashes(store: pathlib.Path) -> dict[int, str]:
    manifest = json.loads((store / "manifest.json").read_text())
    return {f["frame_id"]: f["sha256"] for f in manifest["frames"]}


@pytest.mark.skipif(not FIXTURES.exists(), reason="GeoTrellis fixtures not present")
def test_geotrellis_fixtures_match_what_mabench_writes(tmp_path):
    assert main(["synth", "--out", str(tmp_path / "ramp"), "--width", "50", "--height", "30", "--generator", "ramp",
                 "--bands", "2", "--param", "nodata_block=0,0,5,5", "--tile-size", "16"]) == 0
    assert main(["synth", "--out", str(tmp_path / "normal"), "--width", "61", "--height", "47", "--generator",
                 "normal", "--bands", "2", "--frames", "2", "--seed", "7", "--param", "nodata_fraction=0.2",
                 "--param", "nodata_block=10,10,20,40", "--tile-size", "16"]) == 0
    stores = ["ramp", "normal"]
    for w in WORKLOADS:
        name = "ref/" + w.replace(":", "-")
        assert main(["reference", str(tmp_path / "normal"), "--workload", f"focal:{w}",
                     "--out", str(tmp_path / name)]) == 0
        stores.append(name)
    for s in stores:
        assert _frame_hashes(tmp_path / s) == _frame_hashes(FIXTURES / s), f"fixture {s} is out of date"
