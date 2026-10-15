#!/usr/bin/env bash
# Regenerate the GeoTrellis test fixtures with mabench. Only needed if the tile store format changes;
# mabench/tests/test_fixtures.py fails when the committed fixtures no longer match what mabench writes.
set -euo pipefail
cd "$(dirname "$0")/../geotrellis-baseline/src/test/resources/fixtures"
rm -rf ramp normal ref
mabench synth --out ramp --width 50 --height 30 --generator ramp --bands 2 --param nodata_block=0,0,5,5 --tile-size 16
mabench synth --out normal --width 61 --height 47 --generator normal --bands 2 --frames 2 --seed 7 \
              --param nodata_fraction=0.2 --param nodata_block=10,10,20,40 --tile-size 16
for w in mean:square:2 sum:square:2 min:square:2 max:square:2 mean:circle:3 max:circle:3 mean:square:20; do
  mabench reference normal --workload "focal:$w" --out "ref/${w//:/-}"
done
for s in ramp normal ref/*; do mabench verify "$s" >/dev/null; done
echo "fixtures regenerated"
