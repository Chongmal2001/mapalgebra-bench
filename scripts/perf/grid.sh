#!/usr/bin/env bash
# Performance grids (scripts/perf/grids.txt, benchmark-design.md §4.4), one perf/run.sh per configuration. Stores are
# built on demand (scripts/data/stores.sh). Systems unless the grid line sets SYSTEMS: nes geotrellis (NES with its
# default kernels; nes-exact and nes-running only where a line asks for them)
# plus a NES passthrough (same store, frames per window, threads, variant; run once, then found finished).
#   scripts/perf/grid.sh [ID...]                  default: $GRIDS (bench.env)
#   FILTER="op=max radius=4" scripts/perf/grid.sh G1
# Finished runs are skipped (perf/run.sh), so grids can be stopped, restarted or added later into the same result set.
# Lines with REQUIRES=<feature> run only when the feature is listed in FEATURES. GRIDS_FILE: other definition file.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
cd "$BENCH_REPO"
FILE="${GRIDS_FILE:-$BENCH_REPO/scripts/perf/grids.txt}"
IDS="${*:-$GRIDS}"
STORES="$BENCH_REPO/scripts/data/stores.sh"
export REPS

systems() { echo "nes geotrellis"; }

mapfile -t PLAN < <(python "$BENCH_REPO/scripts/lib/plan.py" "$FILE" "$IDS" "${FILTER:-}")
[[ ${#PLAN[@]} -gt 0 ]] || { echo "no runs for '$IDS' ${FILTER:+(filter $FILTER)}" >&2; exit 2; }
status=0
for line in "${PLAN[@]}"; do
  eval "set -- $line"
  id="$1" store="$2" wl="$3" st="$4"; shift 4
  if [[ "$st" != run ]]; then echo "########## $id $store $wl $*: skipped (${st#skip:} not in FEATURES)"; continue; fi
  (
    for kv in "$@"; do export "${kv%%=*}=${kv#*=}"; done
    "$STORES" "$store" > /dev/null
    path="$("$STORES" --path "$store")"
    IFS=: read -r _ op shape _ <<< "$wl"
    if [[ " ${SYSTEMS:-nes} " == *" nes"* && "${RATE:-0}" == 0 ]]; then
      echo "########## $id passthrough $store frames/window=${WINDOW_FRAMES:-1} threads=${PERF_THREADS:-1} ${VARIANT:-}"
      WINDOW_SLIDE= SYSTEMS=nes-passthrough "$BENCH_REPO/scripts/perf/run.sh" "$path" focal:max:square:1 || exit 1
    fi
    echo "########## $id $store $wl $*"
    SYSTEMS="${SYSTEMS:-$(systems "$shape" "$op")}" "$BENCH_REPO/scripts/perf/run.sh" "$path" "$wl"
  ) || status=1
done
exit $status
