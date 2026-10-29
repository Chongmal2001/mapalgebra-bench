#!/usr/bin/env bash
# Correctness matrix against the GeoTrellis full-grid reference (scripts/run-focal.sh per case), then a summary table.
#   scripts/sweep-correctness.sh STORE [HALO_STORE]
# Per shape in SHAPES, op in OPS and radius in RADII: every summation mode (square sum/mean: exact running kahan;
# otherwise exact), one frame per window; at WINDOW_RADIUS additionally WINDOW_FRAMES frames per window. HALO_STORE
# (small tiles): every shape, op and mode at HALO_RADIUS, so the halo spans more than the 8 neighbouring tiles.
# Defaults: SHAPES="square circle" OPS="sum mean min max" RADII="1 2 4 8 16" WINDOW_RADIUS=4 WINDOW_FRAMES=4
# HALO_RADIUS=20 NES_THREADS=4.
# NES dumps and imported outputs are deleted after each compare; a reference is deleted after its last case
# (KEEP_REFS=1 keeps them). Results: $DATA/results/<store>/*.json, *.png.
set -euo pipefail
[[ $# -ge 1 ]] || { sed -n '2,12p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
export DATA NES_THREADS="${NES_THREADS:-4}" KEEP_DUMP=0 KEEP_OUTPUT=0
STORE="$(realpath "$1")"
HALO_STORE="${2:+$(realpath "$2")}"
SHAPES="${SHAPES:-square circle}"
OPS="${OPS:-sum mean min max}"
RADII="${RADII:-1 2 4 8 16}"
WINDOW_RADIUS="${WINDOW_RADIUS:-4}"
WIN="${WINDOW_FRAMES:-4}"
HALO_RADIUS="${HALO_RADIUS:-20}"
unset WINDOW_FRAMES

modes() { if [[ "$1" == square && ( "$2" == sum || "$2" == mean ) ]]; then echo "exact running kahan"; else echo exact; fi; }
status=0
run_case() {  # store shape op radius frames-per-window
  local m
  for m in $(modes "$2" "$3"); do
    echo "########## $(basename "$1") focal:$3:$2:$4 summation=$m frames/window=$5"
    NES_FOCAL_SUMMATION="$m" WINDOW_FRAMES="$5" scripts/run-focal.sh "$1" "focal:$3:$2:$4" || status=1
  done
}
drop_ref() {  # store shape op radius
  if [[ "${KEEP_REFS:-0}" == 0 ]]; then rm -rf "$DATA/refs/geotrellis/$(basename "$1")/$3-$2-$4"; fi
}

for shape in $SHAPES; do
  for op in $OPS; do
    for r in $RADII; do
      run_case "$STORE" "$shape" "$op" "$r" 1
      if [[ "$r" == "$WINDOW_RADIUS" ]]; then run_case "$STORE" "$shape" "$op" "$r" "$WIN"; fi
      drop_ref "$STORE" "$shape" "$op" "$r"
    done
    if [[ -n "$HALO_STORE" ]]; then
      run_case "$HALO_STORE" "$shape" "$op" "$HALO_RADIUS" 1
      drop_ref "$HALO_STORE" "$shape" "$op" "$HALO_RADIUS"
    fi
  done
done

python - "$DATA/results" "$(basename "$STORE")" ${HALO_STORE:+"$(basename "$HALO_STORE")"} <<'PY'
import json, os, sys
res, stores = sys.argv[1], sys.argv[2:]
print(f"{'store':<22}{'case':<28}{'verdict':>8}{'data cells':>14}{'exact':>10}{'max ULP':>10}{'max abs err':>13}  coverage")
for s in stores:
    d = os.path.join(res, s)
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not f.endswith(".json"):
            continue
        r = json.load(open(os.path.join(d, f))); t = r["totals"]["all"]
        print(f"{s:<22}{f[:-5]:<28}{'PASS' if r['passed'] else 'FAIL':>8}{t['data_both']:>14}{t['exact_rate']:>10.4%}"
              f"{t['max_ulp']:>10.3g}{t['max_abs_error']:>13.3g}  {'; '.join(r['coverage_problems']) or 'ok'}")
PY
exit $status
