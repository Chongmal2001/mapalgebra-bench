#!/usr/bin/env bash
# Throughput of one focal workload at full speed (all frames available at once): NES with a Void sink and the
# GeoTrellis micro-batch baseline (trigger frame, results discarded), REPS runs each, alternating systems.
# System nes-passthrough runs the same NES source and sink without the focal window (cost of source and sink);
# nes runs NES' default kernels (NES_FOCAL_SUMMATION=kahan); nes-exact / nes-running the other Sum / Mean kernels on
# squares (NES_FOCAL_SUMMATION=exact / running, see FocalSpec.hpp).
# Afterwards mabench perf-table collects every run under $RESULTS/perf into $RESULTS/perf.csv.
#
#   scripts/perf/run.sh STORE WORKLOAD          e.g. scripts/perf/run.sh data/stores/s2-berlin-16 focal:mean:square:3
#
# REPS (default 3), SYSTEMS (default "nes geotrellis"; also nes-passthrough, nes-exact, nes-running), PERF_THREADS (NES worker
# threads, default 1), GT_THREADS (GeoTrellis kernel threads, default PERF_THREADS),
# GT_WARMUP (untimed GeoTrellis warm-up frames, default 1), GT_HEAP (sbt, bench.env: 12g), WINDOW_FRAMES (frames per tumbling
# window, default 1; GeoTrellis then triggers per window instead of per frame), WINDOW_SLIDE (NES only: sliding window
# advancing by that many frames; run directory -w<N>s<S>, system name -s<S>), GT_CPUSET (taskset cores for GeoTrellis,
# default DOCKER_CPUSET, which pins NES), GT_JAVA_OPTS (extra JVM options), VARIANT (suffix of the run directory, e.g.
# for runs with other JVM options or with NES_EVENT_TRACE=1), RATE (frames/s, paced input: GeoTrellis --rate; NES
# source FRAME_INTERVAL_US with its pacing.csv and the FrameLatency sink's frame-latency.csv instead of Void; needs a
# NES build with both, FEATURES nes-pacing). WARM_INPUT=1 (default) reads the store into the page cache before each run
# (scripts/lib/warm.sh); NES_WARMUP=1 (default) runs the same query untimed on the store's first frame in the same worker
# before the timed query (warm-up store <store>-warm1, made once with mabench derive). The worker's resource samples
# (resources.csv) and its peak RSS / CPU land next to the timed query's status.json.
# Runs land in $RESULTS/perf/<store>/<workload>[-w<N>][-rate<R>]/<system>-t<threads>[-<variant>]/rep<k>/; finished runs
# are kept and skipped.
set -euo pipefail
[[ $# -eq 2 ]] || { sed -n '2,24p' "$0"; exit 2; }
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
source "$REPO/scripts/lib/common.sh"
STORE="$(realpath "$1")"
WORKLOAD="$2"
W="${WORKLOAD#focal:}"; W="${W//:/-}"
FPW="${WINDOW_FRAMES:-1}"
SLIDE="${WINDOW_SLIDE:-$FPW}"
if [[ "$FPW" != 1 ]]; then W="$W-w$FPW"; fi
if [[ "$SLIDE" != "$FPW" ]]; then W="${W}s$SLIDE"; fi
RATE="${RATE:-0}"
if [[ "$RATE" != 0 ]]; then W="$W-rate$RATE"; fi
PERF="$RESULTS/perf/$(basename "$STORE")/$W"
REPS="${REPS:-3}"
SYSTEMS="${SYSTEMS:-nes geotrellis}"
THREADS="${PERF_THREADS:-1}"
GT_WARMUP="${GT_WARMUP:-1}"
GT_CPUSET="${GT_CPUSET:-$DOCKER_CPUSET}"
GT_PIN=(); if [[ -n "$GT_CPUSET" ]]; then GT_PIN=(taskset -c "$GT_CPUSET"); fi
[[ -e "$STORE/manifest.json" ]] || { echo "error: $STORE is not a tile store" >&2; exit 1; }
WARM_STORE="$DATA/stores/$(basename "$STORE")-warm1"
if [[ " $SYSTEMS " == *" nes"* && "${NES_WARMUP:-1}" == 1 && ! -e "$WARM_STORE/manifest.json" ]]; then
  mabench derive "$STORE" --frames 0:1 --out "$WARM_STORE" --force
fi
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
      nes|nes-passthrough|nes-exact|nes-running) T="$THREADS"; BUILD="$NES_TYPE"; CPUSET="$DOCKER_CPUSET" ;;
      geotrellis) T="${GT_THREADS:-$THREADS}"; BUILD="GT_HEAP=${GT_HEAP:-4g}"; CPUSET="$GT_CPUSET" ;;
      *) echo "error: unknown system $sys" >&2; exit 2 ;;
    esac
    if [[ "$sys" == geotrellis && "$SLIDE" != "$FPW" ]]; then
      echo "== geotrellis: no sliding windows in the micro-batch runner (WINDOW_SLIDE=$SLIDE); skipped" >&2; continue
    fi
    D="$PERF/$sys-t$T${VARIANT:+-$VARIANT}/rep$k"
    if [[ -e "$D/meta.json" ]]; then echo "== $sys t$T rep $k exists"; continue; fi
    rm -rf "$D"; mkdir -p "$D"
    echo "== $sys ($WORKLOAD, threads $T) rep $k/$REPS -> $D"
    if [[ "${WARM_INPUT:-1}" == 1 ]]; then "$REPO/scripts/lib/warm.sh" "$STORE"; fi
    started="$(date -u +%FT%TZ)"
    if [[ "$sys" == nes* ]]; then
      PT=(); if [[ "$sys" == nes-passthrough ]]; then PT=(--passthrough); fi
      SINK=(--void)
      if [[ "$RATE" != 0 ]]; then
        IV="$(python -c 'import sys; print(round(1e6 / float(sys.argv[1])))' "$RATE")"
        SINK=(--latency "$D/frame-latency.csv" --frame-interval-us "$IV" --pacing-log "$D/pacing.csv")
      fi
      mabench nes-query "$STORE" --workload "$WORKLOAD" "${SINK[@]}" --yaml --frames-per-window "$FPW" \
        --slide-frames "$SLIDE" "${PT[@]}" > "$D/topology.yaml"
      SUM=kahan; case "$sys" in nes-exact) SUM=exact ;; nes-running) SUM=running ;; esac
      export NES_THREADS="$T" NES_FOCAL_SUMMATION="$SUM"
      if ! "$REPO/scripts/nes/worker.sh" start "$D"; then status=1; continue; fi
      ok=1
      if [[ "${NES_WARMUP:-1}" == 1 ]]; then
        mkdir -p "$D/warmup"
        mabench nes-query "$WARM_STORE" --workload "$WORKLOAD" --void --yaml --frames-per-window "$FPW" \
          --slide-frames "$SLIDE" "${PT[@]}" > "$D/warmup/topology.yaml"
        NES_WORKER_DIR="$D" "$REPO/scripts/nes/run-query.sh" "$D/warmup/topology.yaml" "$D/warmup" || ok=0
      fi
      if [[ $ok == 1 ]]; then NES_WORKER_DIR="$D" "$REPO/scripts/nes/run-query.sh" "$D/topology.yaml" "$D" || ok=0; fi
      "$REPO/scripts/nes/worker.sh" stop "$D" || true
      unset NES_FOCAL_SUMMATION
      if [[ $ok == 0 ]]; then status=1; continue; fi
    else
      if ! (cd "$REPO/geotrellis-baseline" && "${GT_PIN[@]}" sbt -batch "run --mode microbatch --store $STORE --workload $WORKLOAD \
            --out $D --threads $T --rate $RATE $GT_WINDOW --output none --warmup $GT_WARMUP --no-verify") > "$D/gt.log" 2>&1; then
        tail -n 20 "$D/gt.log" >&2; status=1; continue
      fi
    fi
    cat > "$D/meta.json" <<EOF
{"system": "$sys", "store": "$STORE", "workload": "$WORKLOAD", "threads": $T, "rep": $k, "frames_per_window": $FPW, "slide_frames": $SLIDE,
 "build": "$BUILD", "cpuset": "$CPUSET", "host": "$(hostname)", "started_utc": "$started",
 "warm_input": ${WARM_INPUT:-1}, "nes_warmup": ${NES_WARMUP:-1}, "gt_warmup_frames": $GT_WARMUP,
 "variant": "${VARIANT:-}", "rate_fps": $RATE, "gt_java_opts": "${GT_JAVA_OPTS:-}", "event_trace": ${NES_EVENT_TRACE:-0}}
EOF
  done
done

mabench perf-table "$RESULTS/perf" --out "$RESULTS/perf.csv"
python - "$RESULTS/perf.csv" "$(basename "$STORE")" "$WORKLOAD" "$FPW" "$RATE" <<'EOF'
import csv, sys, statistics as st
path, store, w, fpw, rate = sys.argv[1:]
_, op, shape, r = w.split(":")
rows = [x for x in csv.DictReader(open(path))
        if (x["store"], x["op"], x["shape"], x["radius"], x["frames_per_window"]) == (store, op, shape, r, fpw)
        and float(x["rate_fps"] or 0) == float(rate)]
print(f"{'system':<14}{'threads':>8}{'runs':>6}{'elapsed s':>11}{'Mcells/s':>10}{'CPU s':>8}{'peak RSS MiB':>14}")
med = lambda g, k: st.median(float(x[k]) for x in g if x[k]) if any(x[k] for x in g) else float("nan")
for key in sorted({(x["system"], x["threads"]) for x in rows}):
    g = [x for x in rows if (x["system"], x["threads"]) == key]
    print(f"{key[0]:<14}{key[1]:>8}{len(g):>6}{med(g, 'elapsed_s'):>11.2f}{med(g, 'throughput_cells_per_s') / 1e6:>10.1f}"
          f"{med(g, 'cpu_s'):>8.2f}{med(g, 'peak_rss_bytes') / 2**20:>14.0f}")
EOF
exit $status
