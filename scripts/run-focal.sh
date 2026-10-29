#!/usr/bin/env bash
# One focal workload end to end: reference (GeoTrellis full grid) -> NES run -> import -> compare.
#
#   scripts/run-focal.sh STORE WORKLOAD          e.g. scripts/run-focal.sh data/stores/s2-berlin focal:mean:square:3
#
# Under $DATA (default <repo>/data):
#   refs/<ref-system>/<store>/<workload>/  reference, computed once and reused
#   runs/<store>/<workload>/               topology.yaml, dump.bin, worker.log, status.json, run.json (NES run)
#   out/nes/<store>/<workload>/            NES output imported from the dump
#   results/<store>/<workload>.json/.png   compare report and error heatmap of the first frame
# REF_SYSTEM=geotrellis (default, sbt run --mode fullgrid, heap GT_HEAP) or oracle (mabench reference).
# An existing dump is reused (scripts/clean.sh starts over); NES=0 computes the reference only; KEEP_DUMP=0 deletes the
# dump after import, KEEP_OUTPUT=0 the imported NES output after compare. NES settings: bench.env / bench.local.env
# (scripts/nes/run-query.sh). NES_FOCAL_SUMMATION=running|kahan selects the running-sum Sum/Mean kernel (suffix
# -running / -kahan); WINDOW_FRAMES=N puts N frames into one tumbling window (suffix -wN). The reference is shared.
set -euo pipefail

[[ $# -eq 2 ]] || { sed -n '2,16p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
STORE="$(realpath "$1")"
WORKLOAD="$2"
NAME="$(basename "$STORE")"
W="${WORKLOAD#focal:}"; W="${W//:/-}"
SUMMATION="${NES_FOCAL_SUMMATION:-exact}"
RUNW="$W"; if [[ "$SUMMATION" != exact ]]; then RUNW="$W-$SUMMATION"; fi
FPW="${WINDOW_FRAMES:-1}"
if [[ "$FPW" != 1 ]]; then RUNW="$RUNW-w$FPW"; fi
REF_SYSTEM="${REF_SYSTEM:-geotrellis}"
REF="$DATA/refs/$REF_SYSTEM/$NAME/$W"
RUN="$DATA/runs/$NAME/$RUNW"
NES_OUT="$DATA/out/nes/$NAME/$RUNW"
RES="$DATA/results/$NAME"
mkdir -p "$RES" "$RUN" "$(dirname "$REF")" "$(dirname "$NES_OUT")"
[[ -e "$STORE/manifest.json" ]] || { echo "error: $STORE is not a tile store" >&2; exit 1; }

if [[ -e "$REF/manifest.json" ]]; then
  echo "== reference exists, reusing: $REF"
elif [[ "$REF_SYSTEM" == geotrellis ]]; then
  echo "== GeoTrellis full grid ($WORKLOAD) -> $REF"
  (cd "$REPO/geotrellis-baseline" && sbt -batch "run --mode fullgrid --store $STORE --workload $WORKLOAD --out $REF")
else
  echo "== oracle reference -> $REF"
  mabench reference "$STORE" --workload "$WORKLOAD" --out "$REF"
fi
if [[ "${NES:-1}" == 0 ]]; then exit 0; fi

if [[ -e "$RUN/dump.bin" && -e "$RUN/status.json" ]] && grep -q '"Stopped"' "$RUN/status.json"; then
  echo "== NES dump exists, reusing: $RUN/dump.bin"
else
  echo "== NES ($WORKLOAD) -> $RUN"
  mabench nes-query "$STORE" --workload "$WORKLOAD" --dump "$RUN/dump.bin" --yaml --frames-per-window "$FPW" \
          ${NES_STORE_PATH:+--store-path "$NES_STORE_PATH"} > "$RUN/topology.yaml"
  "$REPO/scripts/nes/run-query.sh" "$RUN/topology.yaml" "$RUN"
fi

echo "== import -> $NES_OUT"
mabench import-nes "$RUN/dump.bin" --input "$STORE" --workload "$WORKLOAD" --out "$NES_OUT" --force
if [[ "${KEEP_DUMP:-1}" == 0 ]]; then rm -f "$RUN/dump.bin"; fi
echo "== compare"
mabench compare "$NES_OUT" --reference "$REF" --input "$STORE" --json "$RES/$RUNW.json" --heatmap "$RES/$RUNW.png"
if [[ "${KEEP_OUTPUT:-1}" == 0 ]]; then rm -rf "$NES_OUT"; fi
