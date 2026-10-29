#!/usr/bin/env bash
# Synthetic smoke-test stores: tiny, readable by eye, for checking that the pipeline (NES run, import-nes, compare)
# works on a machine before real runs. Not evaluation data.
#
#   smoke-1b    8x8,   1 band,  4x4 tiles (2x2 grid, no padding),        3 frames, ramp
#   smoke-2b    10x6,  2 bands, 4x4 tiles (3x2 grid, padded edge tiles), 2 frames, normal + NoData
#   smoke-rgtt  12x12, 1 band,  4x4 tiles (3x3 grid), radius 5 > tile size,  2 frames, waves
#
# Writes $DATA/stores/smoke-*/ and oracle references $DATA/refs/oracle/smoke-*/<workload>/ (run-focal.sh with REF_SYSTEM=oracle).
# Frames are 100 ms apart. Usage: scripts/gen-smoke-stores.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
S="$DATA/stores"; R="$DATA/refs/oracle"
mkdir -p "$S" "$R"

mabench synth --force --out "$S/smoke-1b" --width 8 --height 8 --generator ramp --frames 3 --frame-period-ms 100 --tile-size 4
mabench synth --force --out "$S/smoke-2b" --width 10 --height 6 --generator normal --bands 2 --frames 2 --frame-period-ms 100 \
              --seed 5 --param nodata_block=0,0,2,2 --param nodata_fraction=0.1 --tile-size 4
mabench synth --force --out "$S/smoke-rgtt" --width 12 --height 12 --generator waves --frames 2 --frame-period-ms 100 --tile-size 4

for s in smoke-1b smoke-2b; do
  for w in sum:square:1 mean:circle:2; do
    mabench reference "$S/$s" --workload "focal:$w" --out "$R/$s/${w//:/-}" --force
  done
done
mabench reference "$S/smoke-rgtt" --workload focal:sum:square:5 --out "$R/smoke-rgtt/sum-square-5" --force
for d in "$S"/smoke-* "$R"/smoke-*/*/; do mabench verify "$d" >/dev/null; done
echo "smoke stores in $S/smoke-*, references in $R/smoke-*"
