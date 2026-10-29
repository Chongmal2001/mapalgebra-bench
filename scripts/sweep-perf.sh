#!/usr/bin/env bash
# Performance matrix (scripts/run-perf.sh per case): NES and the GeoTrellis micro-batch baseline at full speed.
#   scripts/sweep-perf.sh STORE
# Per frames-per-window in WINDOWS: a NES passthrough, then per shape in SHAPES, op in OPS and radius in RADII:
#   square sum/mean: nes nes-running nes-kahan geotrellis      otherwise: nes geotrellis
# Defaults: SHAPES="square circle" OPS="max mean" RADII="1 2 4 8 16" WINDOWS="1 4" REPS=2 (PERF_THREADS, GT_HEAP as in
# run-perf.sh).
# Finished runs are skipped, so the sweep can be stopped and restarted. Results: $DATA/results/perf.csv.
set -euo pipefail
[[ $# -eq 1 ]] || { sed -n '2,9p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
STORE="$1"
SHAPES="${SHAPES:-square circle}"
OPS="${OPS:-max mean}"
RADII="${RADII:-1 2 4 8 16}"
WINDOWS="${WINDOWS:-1 4}"
export REPS="${REPS:-2}"

systems() {
  if [[ "$1" == square && ( "$2" == sum || "$2" == mean ) ]]; then echo "nes nes-running nes-kahan geotrellis"
  else echo "nes geotrellis"; fi
}
status=0
for fpw in $WINDOWS; do
  echo "########## passthrough, frames/window=$fpw"
  WINDOW_FRAMES="$fpw" SYSTEMS=nes-passthrough scripts/run-perf.sh "$STORE" focal:max:square:1 || status=1
  for shape in $SHAPES; do
    for op in $OPS; do
      for r in $RADII; do
        echo "########## focal:$op:$shape:$r frames/window=$fpw"
        WINDOW_FRAMES="$fpw" SYSTEMS="$(systems "$shape" "$op")" scripts/run-perf.sh "$STORE" "focal:$op:$shape:$r" || status=1
      done
    done
  done
done
exit $status
