#!/usr/bin/env bash
# Performance matrix (scripts/run-perf.sh per case): NES and the GeoTrellis micro-batch baseline at full speed.
#   scripts/sweep-perf.sh STORE
# Per frames-per-window in WINDOWS: a NES passthrough, then per op in OPS and radius in RADII the systems of that op:
#   max/min: nes geotrellis      sum/mean: nes nes-running nes-kahan geotrellis
# Defaults: OPS="max mean" RADII="1 2 4 8 16" WINDOWS="1 4" REPS=2 (PERF_THREADS, GT_HEAP as in run-perf.sh).
# Finished runs are skipped, so the sweep can be stopped and restarted. Results: $DATA/results/perf.csv.
set -euo pipefail
[[ $# -eq 1 ]] || { sed -n '2,8p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
STORE="$1"
OPS="${OPS:-max mean}"
RADII="${RADII:-1 2 4 8 16}"
WINDOWS="${WINDOWS:-1 4}"
export REPS="${REPS:-2}"

systems() { case "$1" in sum|mean) echo "nes nes-running nes-kahan geotrellis" ;; *) echo "nes geotrellis" ;; esac; }
status=0
for fpw in $WINDOWS; do
  echo "########## passthrough, frames/window=$fpw"
  WINDOW_FRAMES="$fpw" SYSTEMS=nes-passthrough scripts/run-perf.sh "$STORE" focal:max:square:1 || status=1
  for op in $OPS; do
    for r in $RADII; do
      echo "########## focal:$op:square:$r frames/window=$fpw"
      WINDOW_FRAMES="$fpw" SYSTEMS="$(systems "$op")" scripts/run-perf.sh "$STORE" "focal:$op:square:$r" || status=1
    done
  done
done
exit $status
