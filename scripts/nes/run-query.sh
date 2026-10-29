#!/usr/bin/env bash
# Runs one nes-cli topology in a fresh container (worker + query, then exit) and waits for the query to end.
#   scripts/nes/run-query.sh TOPOLOGY.yaml OUT_DIR
# OUT_DIR gets status.json (final query status incl. started/stopped in µs), worker.log, cli.log, run.json,
# worker_vmhwm (peak RSS of the worker). NES_PROFILE_SECS > 0 also samples the worker with perf (scripts/nes/profile.sh).
# Exit code: 0 query Stopped, 1 Failed or worker died, 3 timeout.
set -euo pipefail
source "$(dirname "$0")/common.sh"

[[ $# -eq 2 ]] || { sed -n '2,5p' "$0"; exit 2; }
TOPO="$(realpath "$1")"
OUT="$(realpath -m "$2")"
mkdir -p "$OUT"
[[ -x "$NES_WORKER_BIN" && -x "$NES_CLI_BIN" ]] || { echo "error: NES binaries missing, run scripts/nes/build.sh" >&2; exit 2; }
grep -q 'localhost:8080' "$TOPO" || { echo "error: $TOPO must use worker localhost:8080" >&2; exit 2; }

cat > "$OUT/run.json" <<EOF
{
  "topology": "$TOPO",
  "nes_commit": "$(git -C "$NES_REPO" rev-parse HEAD 2>/dev/null)",
  "nes_dirty": $( [[ -n "$(git -C "$NES_REPO" status --porcelain 2>/dev/null)" ]] && echo true || echo false),
  "bench_commit": "$(git -C "$BENCH_REPO" rev-parse HEAD 2>/dev/null)",
  "image": "$(docker image inspect --format '{{.Id}}' "$DOCKER_IMAGE")",
  "docker_context": "$(docker context show 2>/dev/null)",
  "docker_host_os": "$(docker info --format '{{.OperatingSystem}}' 2>/dev/null)",
  "worker_bin": "$NES_WORKER_BIN",
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

set +e
docker_run bash -s <<EOF
W='$NES_WORKER_BIN'; CLI='$NES_CLI_BIN'; OUT='$OUT'; TOPO='$TOPO'
"\$W" --grpc=localhost:8080 --data_address=localhost:9090 \
  --worker.number_of_buffers_in_global_buffer_manager=$NES_POOL_BUFFERS \
  --worker.query_engine.number_of_worker_threads=$NES_THREADS \
  --worker.default_max_inflight_buffers=$NES_MAX_INFLIGHT $NES_WORKER_ARGS > "\$OUT/worker.log" 2>&1 &
WPID=\$!
for i in \$(seq 600); do
  (: < /dev/tcp/localhost/8080) 2>/dev/null && break
  kill -0 \$WPID 2>/dev/null || { echo "worker exited during startup, see \$OUT/worker.log" >&2; exit 1; }
  sleep 0.1
done
(: < /dev/tcp/localhost/8080) 2>/dev/null || { echo "worker not listening on 8080 after 60 s" >&2; kill \$WPID; exit 1; }
ID=\$("\$CLI" -t "\$TOPO" start 2>>"\$OUT/cli.log" | tail -n 1)
[[ -n "\$ID" ]] || { echo "nes-cli start failed, see \$OUT/cli.log" >&2; kill \$WPID; exit 1; }
echo "query \$ID" >&2
PERFPID=
if (( ${NES_PROFILE_SECS:-0} > 0 )); then
  PERF=\$(ls /usr/lib/linux-tools/*/perf 2>/dev/null | head -n 1)
  (sleep 3; "\$PERF" record -F 999 -g -p \$WPID -o "\$OUT/perf.data" -- sleep ${NES_PROFILE_SECS:-0}) > "\$OUT/perf.log" 2>&1 &
  PERFPID=\$!
fi
END=\$(( \$(date +%s) + $NES_TIMEOUT_S ))
RC=
while [[ -z "\$RC" ]]; do
  "\$CLI" -t "\$TOPO" status "\$ID" > "\$OUT/status.json" 2>>"\$OUT/cli.log"
  if ! grep -qE '"query_status": *"(Registered|Started|Running)"' "\$OUT/status.json"; then
    grep VmHWM /proc/\$WPID/status > "\$OUT/worker_vmhwm" 2>/dev/null
    kill \$WPID; wait \$WPID 2>/dev/null
    if grep -qE '"query_status": *"Stopped"' "\$OUT/status.json"; then RC=0; else RC=1; fi
  elif ! kill -0 \$WPID 2>/dev/null; then
    echo "worker died, see \$OUT/worker.log" >&2; RC=1
  elif (( \$(date +%s) >= END )); then
    "\$CLI" -t "\$TOPO" stop "\$ID" >>"\$OUT/cli.log" 2>&1; kill \$WPID; RC=3
  else
    sleep $NES_POLL_S
  fi
done
if [[ -n "\$PERFPID" ]]; then
  wait \$PERFPID
  "\$PERF" report -i "\$OUT/perf.data" --stdio --no-children -g none --sort dso,sym --percent-limit 0.5 \
    > "\$OUT/perf-report.txt" 2>>"\$OUT/perf.log"
fi
if [[ -n "${HOST_IDS:-}" ]]; then chown -R ${HOST_IDS:-} "\$OUT"; fi
exit \$RC
EOF
rc=$?
set -e
case $rc in
  0) echo "stopped: $OUT/status.json" ;;
  3) echo "timeout after ${NES_TIMEOUT_S}s: $OUT" >&2 ;;
  *) echo "failed (exit $rc): $OUT/status.json, $OUT/worker.log" >&2 ;;
esac
exit $rc
