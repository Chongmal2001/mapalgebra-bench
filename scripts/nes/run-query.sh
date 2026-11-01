#!/usr/bin/env bash
# Runs one nes-cli topology and waits for the query to end. With NES_WORKER_DIR the query goes to that running worker
# (scripts/nes/worker.sh start); otherwise a worker is started in OUT_DIR for this query and stopped after it.
#   scripts/nes/run-query.sh TOPOLOGY.yaml OUT_DIR
# OUT_DIR gets status.json (final query status incl. running/stopped in µs), cli.log, run.json; with its own worker also
# worker.log, resources.csv, worker_vmhwm, worker_stat. NES_PROFILE_SECS > 0 also samples the worker with perf
# (scripts/nes/profile.sh). Exit code: 0 query Stopped, 1 Failed or worker died, 3 timeout.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"

[[ $# -eq 2 ]] || { sed -n '2,7p' "$0"; exit 2; }
TOPO="$(realpath "$1")"
OUT="$(realpath -m "$2")"
WORKER="$BENCH_REPO/scripts/nes/worker.sh"
mkdir -p "$OUT"
grep -q 'localhost:8080' "$TOPO" || { echo "error: $TOPO must use worker localhost:8080" >&2; exit 2; }

if [[ "$NES_NATIVE" == 1 ]]; then
  # shellcheck disable=SC1091
  source "$NES_BUNDLE/BUILD_INFO"
  RUNTIME=native NES_COMMIT="$BUNDLE_NES_COMMIT" NES_DIRTY="$BUNDLE_NES_DIRTY" IMAGE="$BUNDLE_IMAGE" CTX=none
  HOST_OS="$(. /etc/os-release && echo "$PRETTY_NAME")"
else
  RUNTIME=docker NES_COMMIT="$(git -C "$NES_REPO" rev-parse HEAD 2>/dev/null || true)"
  NES_DIRTY=$( [[ -n "$(git -C "$NES_REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]] && echo true || echo false)
  IMAGE="$(docker image inspect --format '{{.Id}}' "$DOCKER_IMAGE")"
  CTX="$(docker context show 2>/dev/null)" HOST_OS="$(docker info --format '{{.OperatingSystem}}' 2>/dev/null)"
fi

WD="${NES_WORKER_DIR:-}"
if [[ -z "$WD" ]]; then
  WD="$OUT"
  "$WORKER" start "$WD"
  trap '"$WORKER" stop "$WD"' EXIT
fi
WD="$(realpath "$WD")"

cat > "$OUT/run.json" <<EOF
{
  "topology": "$TOPO",
  "runtime": "$RUNTIME",
  "nes_commit": "$NES_COMMIT",
  "nes_dirty": $NES_DIRTY,
  "bench_commit": "$(git -C "$BENCH_REPO" rev-parse HEAD 2>/dev/null)",
  "image": "$IMAGE",
  "docker_context": "$CTX",
  "docker_host_os": "$HOST_OS",
  "worker_bin": "$NES_WORKER_BIN",
  "worker_dir": "$WD",
  "pool_buffers": $NES_POOL_BUFFERS,
  "threads": $NES_THREADS,
  "max_inflight_buffers": $NES_MAX_INFLIGHT,
  "focal_summation": "${NES_FOCAL_SUMMATION:-exact}",
  "worker_args": "$NES_WORKER_ARGS",
  "docker_memory": "$DOCKER_MEMORY",
  "docker_cpuset": "$DOCKER_CPUSET",
  "host": "$(hostname)",
  "submitted_utc": "$(date -u +%FT%TZ)"
}
EOF

cli() { "$WORKER" exec "$WD" "$NES_CLI_BIN" -t "$TOPO" "$@"; }
ID="$(cli start 2>>"$OUT/cli.log" | tail -n 1)" || true
[[ -n "$ID" ]] || { echo "nes-cli start failed, see $OUT/cli.log" >&2; exit 1; }
echo "query $ID" >&2

PERFPID=
if (( ${NES_PROFILE_SECS:-0} > 0 )); then
  WPID="$("$WORKER" pid "$WD")"
  (sleep 3; "$WORKER" exec "$WD" bash -c "P=\$(ls /usr/lib/linux-tools/*/perf | head -n 1); \"\$P\" record -F 999 -g \
    -p $WPID -o '$OUT/perf.data' -- sleep $NES_PROFILE_SECS") > "$OUT/perf.log" 2>&1 &
  PERFPID=$!
fi

END=$(( $(date +%s) + NES_TIMEOUT_S ))
RC=
while [[ -z "$RC" ]]; do
  cli status "$ID" > "$OUT/status.json" 2>>"$OUT/cli.log" || true
  if ! grep -qE '"query_status": *"(Registered|Started|Running)"' "$OUT/status.json"; then
    if grep -qE '"query_status": *"Stopped"' "$OUT/status.json"; then RC=0; else RC=1; fi
  elif ! "$WORKER" alive "$WD"; then
    echo "worker died, see $WD/worker.log" >&2; RC=1
  elif (( $(date +%s) >= END )); then
    cli stop "$ID" >>"$OUT/cli.log" 2>&1 || true; RC=3
  else
    sleep "$NES_POLL_S"
  fi
done
if [[ -n "$PERFPID" ]]; then
  wait "$PERFPID" || true
  "$WORKER" exec "$WD" bash -c "P=\$(ls /usr/lib/linux-tools/*/perf | head -n 1); \"\$P\" report -i '$OUT/perf.data' \
    --stdio --no-children -g none --sort dso,sym --percent-limit 0.5" > "$OUT/perf-report.txt" 2>>"$OUT/perf.log" || true
fi
case $RC in
  0) echo "stopped: $OUT/status.json" ;;
  3) echo "timeout after ${NES_TIMEOUT_S}s: $OUT" >&2 ;;
  *) echo "failed (exit $RC): $OUT/status.json, $WD/worker.log" >&2 ;;
esac
exit $RC
