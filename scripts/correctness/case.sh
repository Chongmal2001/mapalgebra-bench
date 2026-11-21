#!/usr/bin/env bash
# One focal workload end to end: reference (GeoTrellis full grid) -> NES run -> import -> compare.
#
#   scripts/correctness/case.sh STORE WORKLOAD     e.g. scripts/correctness/case.sh data/stores/s2-berlin-16 focal:mean:square:3
#
# Under $DATA: refs/<ref-system>/<store>/<workload>/ (reference, computed once and reused), out/nes/<store>/<workload>/
# (NES output imported from the dump). Under $RESULTS (bench.env, per RUN_LABEL), with CASE_ID (default adhoc):
#   runs/<case>/<store>/<run>/                  topology.yaml, dump.bin, worker.log, status.json, run.json (NES run)
#   correctness/<case>/<store>/<run>.json/.png  compare report and error heatmap of the first frame
# <run> = workload, then -<summation>, -w<frames per window>[s<slide>], -<CASE_TAG> where set.
# NES_INPUT=STORE2: NES reads STORE2 instead of STORE (same frames); reference, import and compare use STORE.
# REF_SYSTEM=geotrellis (default, sbt run --mode fullgrid, heap GT_HEAP, GT_REF_THREADS threads) or oracle (mabench reference).
# An existing dump is reused (scripts/clean.sh starts over); NES=0 computes the reference only; KEEP_DUMP=0 deletes the
# dump after import, KEEP_OUTPUT=0 the imported NES output after compare. NES settings: bench.env / bench.local.env
# (scripts/nes/run-query.sh). NES_FOCAL_SUMMATION=exact|running|kahan selects the Sum/Mean kernel (default kahan;
# suffix -running / -kahan, none for exact); WINDOW_FRAMES=N puts N frames into one tumbling window (suffix -wN); WINDOW_SLIDE=S makes it a
# sliding window advancing by S frames (suffix -wNsS; every output tile must then appear once per window containing
# its frame, N/S times except for frames in the first window). The reference is shared. PACE_US=N: paced NES source
# (one frame every N µs, buffers cut at frame boundaries; needs a NES build with pacing); results must not change.
set -euo pipefail

[[ $# -eq 2 ]] || { sed -n '2,20p' "$0"; exit 2; }
source "$(dirname "$0")/../lib/common.sh"
REPO="$BENCH_REPO"
STORE="$(realpath "$1")"
WORKLOAD="$2"
NAME="$(basename "$STORE")"
W="${WORKLOAD#focal:}"; W="${W//:/-}"
SUMMATION="${NES_FOCAL_SUMMATION:-kahan}"
RUNW="$W"; if [[ "$SUMMATION" != exact ]]; then RUNW="$W-$SUMMATION"; fi
FPW="${WINDOW_FRAMES:-1}"
SLIDE="${WINDOW_SLIDE:-$FPW}"
(( SLIDE >= 1 && FPW % SLIDE == 0 )) || { echo "error: WINDOW_SLIDE must divide WINDOW_FRAMES" >&2; exit 2; }
if [[ "$FPW" != 1 || "$SLIDE" != "$FPW" ]]; then RUNW="$RUNW-w$FPW"; fi
if [[ "$SLIDE" != "$FPW" ]]; then RUNW="${RUNW}s$SLIDE"; fi
if [[ -n "${CASE_TAG:-}" ]]; then RUNW="$RUNW-$CASE_TAG"; fi
CASE="${CASE_ID:-adhoc}"
NES_STORE="$(realpath "${NES_INPUT:-$STORE}")"
REF_SYSTEM="${REF_SYSTEM:-geotrellis}"
REF="$DATA/refs/$REF_SYSTEM/$NAME/$W"
RUN="$RESULTS/runs/$CASE/$NAME/$RUNW"
NES_OUT="$DATA/out/nes/$CASE/$NAME/$RUNW"
RES="$RESULTS/correctness/$CASE/$NAME"
mkdir -p "$RES" "$RUN" "$(dirname "$REF")" "$(dirname "$NES_OUT")"
[[ -e "$STORE/manifest.json" ]] || { echo "error: $STORE is not a tile store" >&2; exit 1; }

if [[ -e "$REF/manifest.json" ]]; then
  echo "== reference exists, reusing: $REF"
elif [[ "$REF_SYSTEM" == geotrellis ]]; then
  echo "== GeoTrellis full grid ($WORKLOAD) -> $REF"
  (cd "$REPO/geotrellis-baseline" && sbt -batch "run --mode fullgrid --store $STORE --workload $WORKLOAD --out $REF --threads $GT_REF_THREADS")
else
  echo "== oracle reference -> $REF"
  mabench reference "$STORE" --workload "$WORKLOAD" --out "$REF"
fi
if [[ "${NES:-1}" == 0 ]]; then exit 0; fi

if [[ -e "$RUN/dump.bin" && -e "$RUN/status.json" ]] && grep -q '"Stopped"' "$RUN/status.json"; then
  echo "== NES dump exists, reusing: $RUN/dump.bin"
else
  echo "== NES ($WORKLOAD) -> $RUN"
  mabench nes-query "$NES_STORE" --workload "$WORKLOAD" --dump "$RUN/dump.bin" --yaml --frames-per-window "$FPW" \
          --slide-frames "$SLIDE" ${NES_STORE_PATH:+--store-path "$NES_STORE_PATH"} \
          ${PACE_US:+--frame-interval-us "$PACE_US" --pacing-log "$RUN/pacing.csv"} > "$RUN/topology.yaml"
  "$REPO/scripts/nes/run-query.sh" "$RUN/topology.yaml" "$RUN"
fi

echo "== import -> $NES_OUT"
mabench import-nes "$RUN/dump.bin" --input "$STORE" --workload "$WORKLOAD" --out "$NES_OUT" --force \
  --frames-per-window "$FPW" --slide-frames "$SLIDE"
if [[ "${KEEP_DUMP:-1}" == 0 ]]; then rm -f "$RUN/dump.bin"; fi
echo "== compare"
mabench compare "$NES_OUT" --reference "$REF" --input "$STORE" --json "$RES/$RUNW.json" --heatmap "$RES/$RUNW.png"
if [[ "${KEEP_OUTPUT:-1}" == 0 ]]; then rm -rf "$NES_OUT"; fi
