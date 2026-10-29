#!/usr/bin/env bash
# Throughput of one focal workload at full speed (all frames available at once): NES with a Void sink and the
# GeoTrellis micro-batch baseline (trigger frame, checksum output), REPS runs each, alternating systems.
# System nes-passthrough runs the same NES source and sink without the focal window (cost of source and sink);
# nes-running / nes-kahan run NES with NES_FOCAL_SUMMATION=running / kahan (Sum/Mean on squares, see FocalSpec.hpp).
# Afterwards mabench perf-table collects every run under $DATA/perf into $DATA/results/perf.csv.
#
#   scripts/run-perf.sh STORE WORKLOAD          e.g. scripts/run-perf.sh data/stores/s2-berlin focal:mean:square:3
#
# REPS (default 3), SYSTEMS (default "nes geotrellis"; also nes-passthrough, nes-running, nes-kahan), PERF_THREADS (NES worker threads, default 1),
# GT_WARMUP (untimed GeoTrellis warm-up frames, default 1), GT_HEAP (sbt, default 4g), WINDOW_FRAMES (frames per tumbling
# window, default 1; GeoTrellis then triggers per window instead of per frame).
# Runs land in $DATA/perf/<store>/<workload>[-w<N>]/<system>-t<threads>/rep<k>/; finished runs are kept and skipped.
set -euo pipefail
[[ $# -eq 2 ]] || { sed -n '2,14p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/.." && pwd)"
source "$REPO/scripts/nes/common.sh"
DATA="$(realpath -m "$DATA")"
STORE="$(realpath "$1")"
WORKLOAD="$2"
W="${WORKLOAD#focal:}"; W="${W//:/-}"
FPW="${WINDOW_FRAMES:-1}"
if [[ "$FPW" != 1 ]]; then W="$W-w$FPW"; fi
PERF="$DATA/perf/$(basename "$STORE")/$W"
REPS="${REPS:-3}"
SYSTEMS="${SYSTEMS:-nes geotrellis}"
THREADS="${PERF_THREADS:-1}"
GT_WARMUP="${GT_WARMUP:-1}"
[[ -e "$STORE/manifest.json" ]] || { echo "error: $STORE is not a tile store" >&2; exit 1; }
GT_WINDOW="--trigger frame"
if [[ "$FPW" != 1 ]]; then
  STEP="$(python -c 'import json, sys
t = sorted(f["event_time_ms"] for f in json.load(open(sys.argv[1]))["frames"])
print(min([b - a for a, b in zip(t, t[1:]) if b > a] or [1000]))' "$STORE/manifest.json")"
  GT_WINDOW="--trigger window --window-ms $((STEP * FPW))"
fi

NES_TYPE="$(sed -n 's/^CMAKE_BUILD_TYPE:STRING=//p' "$NES_BUILD/CMakeCache.txt" 2>/dev/null || true)"
if [[ " $SYSTEMS " == *" nes"* && "$NES_TYPE" != Release ]]; then
  echo "warning: NES build $NES_BUILD is '${NES_TYPE:-unknown}', not Release" >&2
fi

status=0
for k in $(seq 1 "$REPS"); do
  for sys in $SYSTEMS; do
    case "$sys" in
      nes|nes-passthrough|nes-running|nes-kahan) T="$THREADS"; BUILD="$NES_TYPE" ;;
      geotrellis) T=1; BUILD="GT_HEAP=${GT_HEAP:-4g}" ;;
      *) echo "error: unknown system $sys" >&2; exit 2 ;;
    esac
    D="$PERF/$sys-t$T/rep$k"
    if [[ -e "$D/meta.json" ]]; then echo "== $sys t$T rep $k exists"; continue; fi
    rm -rf "$D"; mkdir -p "$D"
    echo "== $sys ($WORKLOAD, threads $T) rep $k/$REPS -> $D"
    started="$(date -u +%FT%TZ)"
    if [[ "$sys" == nes* ]]; then
      PT=(); if [[ "$sys" == nes-passthrough ]]; then PT=(--passthrough); fi
      mabench nes-query "$STORE" --workload "$WORKLOAD" --void --yaml --frames-per-window "$FPW" "${PT[@]}" \
        > "$D/topology.yaml"
      SUM=exact; case "$sys" in nes-running) SUM=running ;; nes-kahan) SUM=kahan ;; esac
      if ! NES_THREADS="$T" NES_FOCAL_SUMMATION="$SUM" "$REPO/scripts/nes/run-query.sh" "$D/topology.yaml" "$D"; then
        status=1; continue
      fi
    else
      if ! (cd "$REPO/geotrellis-baseline" && sbt -batch "run --mode microbatch --store $STORE --workload $WORKLOAD \
            --out $D --rate 0 $GT_WINDOW --output checksum --warmup $GT_WARMUP --no-verify") > "$D/gt.log" 2>&1; then
        tail -n 20 "$D/gt.log" >&2; status=1; continue
      fi
    fi
    cat > "$D/meta.json" <<EOF
{"system": "$sys", "store": "$STORE", "workload": "$WORKLOAD", "threads": $T, "rep": $k, "frames_per_window": $FPW,
 "build": "$BUILD", "host": "$(hostname)", "started_utc": "$started"}
EOF
  done
done

mabench perf-table "$DATA/perf" --out "$DATA/results/perf.csv"
python - "$DATA/results/perf.csv" "$(basename "$STORE")" "$WORKLOAD" "$FPW" <<'EOF'
import csv, sys, statistics as st
path, store, w, fpw = sys.argv[1:]
_, op, shape, r = w.split(":")
rows = [x for x in csv.DictReader(open(path))
        if (x["store"], x["op"], x["shape"], x["radius"], x["frames_per_window"]) == (store, op, shape, r, fpw)]
print(f"{'system':<14}{'threads':>8}{'runs':>6}{'wall s (median)':>18}{'Mcells/s (median)':>20}{'peak mem MiB':>14}")
for key in sorted({(x["system"], x["threads"]) for x in rows}):
    g = [x for x in rows if (x["system"], x["threads"]) == key]
    mem = [float(x["peak_mem_bytes"]) for x in g if x["peak_mem_bytes"]]
    print(f"{key[0]:<14}{key[1]:>8}{len(g):>6}{st.median(float(x['wall_s']) for x in g):>18.2f}"
          f"{st.median(float(x['throughput_cells_per_s']) for x in g) / 1e6:>20.1f}"
          f"{(max(mem) / 2**20 if mem else float('nan')):>14.0f}")
EOF
exit $status
