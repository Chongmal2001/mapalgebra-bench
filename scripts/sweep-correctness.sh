#!/usr/bin/env bash
# Correctness matrix against the GeoTrellis full-grid reference (scripts/run-focal.sh per case), then a summary table.
#   scripts/sweep-correctness.sh STORE [HALO_STORE]
# Per op in OPS and radius in RADII: every summation mode (sum/mean: exact running kahan; min/max: exact), one frame per
# window; at WINDOW_RADIUS additionally WINDOW_FRAMES frames per window. HALO_STORE (small tiles): every op and mode at
# HALO_RADIUS, so the halo spans more than the 8 neighbouring tiles.
# Defaults: OPS="sum mean min max" RADII="1 2 4 8 16" WINDOW_RADIUS=4 WINDOW_FRAMES=4 HALO_RADIUS=20 NES_THREADS=4.
# NES dumps and imported outputs are deleted after each compare; a reference is deleted after its last case
# (KEEP_REFS=1 keeps them). Results: $DATA/results/<store>/*.json, *.png.
set -euo pipefail
[[ $# -ge 1 ]] || { sed -n '2,11p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
export DATA NES_THREADS="${NES_THREADS:-4}" KEEP_DUMP=0 KEEP_OUTPUT=0
STORE="$(realpath "$1")"
HALO_STORE="${2:+$(realpath "$2")}"
OPS="${OPS:-sum mean min max}"
RADII="${RADII:-1 2 4 8 16}"
WINDOW_RADIUS="${WINDOW_RADIUS:-4}"
WIN="${WINDOW_FRAMES:-4}"
HALO_RADIUS="${HALO_RADIUS:-20}"
unset WINDOW_FRAMES

modes() { case "$1" in sum|mean) echo "exact running kahan" ;; *) echo exact ;; esac; }
status=0
run_case() {  # store op radius frames-per-window
  local m
  for m in $(modes "$2"); do
    echo "########## $(basename "$1") focal:$2:square:$3 summation=$m frames/window=$4"
    NES_FOCAL_SUMMATION="$m" WINDOW_FRAMES="$4" scripts/run-focal.sh "$1" "focal:$2:square:$3" || status=1
  done
}
drop_ref() {  # store op radius
  if [[ "${KEEP_REFS:-0}" == 0 ]]; then rm -rf "$DATA/refs/geotrellis/$(basename "$1")/$2-square-$3"; fi
}

for op in $OPS; do
  for r in $RADII; do
    run_case "$STORE" "$op" "$r" 1
    if [[ "$r" == "$WINDOW_RADIUS" ]]; then run_case "$STORE" "$op" "$r" "$WIN"; fi
    drop_ref "$STORE" "$op" "$r"
  done
  if [[ -n "$HALO_STORE" ]]; then
    run_case "$HALO_STORE" "$op" "$HALO_RADIUS" 1
    drop_ref "$HALO_STORE" "$op" "$HALO_RADIUS"
  fi
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
