#!/usr/bin/env bash
# One focal workload end to end: GeoTrellis baseline -> oracle reference -> compare.
#
#   scripts/run-focal.sh STORE WORKLOAD          e.g. scripts/run-focal.sh data/stores/goes19 focal:mean:square:3
#
# Outputs (under $DATA, default <repo>/data):
#   out/gt/<store>/<workload>/        GeoTrellis output store + timings.csv
#   refs/<store>/<workload>/          oracle reference (computed once, reused afterwards)
#   results/<store>/<workload>.json   compare report;  .png  error heatmap of the first frame
# Heap for GeoTrellis: GT_HEAP (default 4g), e.g. GT_HEAP=20g for GOES C02 frames.
set -euo pipefail

[[ $# -eq 2 ]] || { sed -n '2,10p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
STORE="$(realpath "$1")"
WORKLOAD="$2"
NAME="$(basename "$STORE")"
W="${WORKLOAD#focal:}"; W="${W//:/-}"

OUT="$DATA/out/gt/$NAME/$W"
REF="$DATA/refs/$NAME/$W"
RES="$DATA/results/$NAME"
mkdir -p "$RES" "$(dirname "$OUT")"

if [[ -e "$OUT/manifest.json" ]]; then
  echo "== GeoTrellis output exists, skipping: $OUT"
else
  echo "== GeoTrellis ($WORKLOAD) -> $OUT"
  (cd "$REPO/geotrellis-baseline" && sbt -batch "run --store $STORE --workload $WORKLOAD --out $OUT")
fi

if [[ -e "$REF/manifest.json" ]]; then
  echo "== reference exists, reusing: $REF"
else
  echo "== oracle reference -> $REF"
  mabench reference "$STORE" --workload "$WORKLOAD" --out "$REF"
fi

echo "== compare"
mabench compare "$OUT" --reference "$REF" --input "$STORE" --json "$RES/$W.json" --heatmap "$RES/$W.png"
