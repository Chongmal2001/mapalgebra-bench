#!/usr/bin/env bash
# One NES single-node worker that outlives single queries: on the host (NES_NATIVE=1) or in a detached container.
#   scripts/nes/worker.sh start DIR       worker.log, resources.csv (scripts/lib/sample.sh every NES_SAMPLE_MS), worker.env
#   scripts/nes/worker.sh stop DIR        worker_vmhwm (peak RSS of the worker's life), worker_stat (/proc/<pid>/stat at
#                                         the end, with clk_tck), then stops sampler and worker
#   scripts/nes/worker.sh exec DIR CMD…   runs CMD where the worker runs (host or its container)
#   scripts/nes/worker.sh alive|pid DIR
# Worker settings (pool, threads, in-flight buffers, NES_FOCAL_SUMMATION, pinning) as in bench.env. NES_EVENT_TRACE=1:
# the worker writes a Chrome trace (trace_*.json in DIR) with every task's start and end per pipeline.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
[[ $# -ge 2 ]] || { sed -n '2,9p' "$0"; exit 2; }
CMD="$1"; DIR="$(realpath -m "$2")"; shift 2
ENVF="$DIR/worker.env"

load() {
  [[ -f "$ENVF" ]] || { echo "error: no worker in $DIR (scripts/nes/worker.sh start $DIR)" >&2; exit 2; }
  # shellcheck disable=SC1090
  source "$ENVF"
}
in_worker() {
  if [[ -z "$CONTAINER" ]]; then "$@"; return; fi
  local priv=(); if [[ " ${DOCKER_RUN_ARGS:-} " == *" --privileged "* ]]; then priv=(--privileged); fi
  docker exec -i "${priv[@]}" --user "$DOCKER_USER" -e HOME=/tmp "$CONTAINER" "$@"
}
alive() {
  if [[ -z "$CONTAINER" ]]; then kill -0 "$WORKER_PID" 2>/dev/null
  else [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == true ]]; fi
}

case "$CMD" in
  start)
    [[ -x "$NES_WORKER_BIN" && -x "$NES_CLI_BIN" ]] || { echo "error: NES binaries missing, run scripts/nes/build.sh (native: scripts/nes/bundle.sh)" >&2; exit 2; }
    [[ ! -e "$ENVF" ]] || { echo "error: $ENVF exists (worker running? scripts/nes/worker.sh stop $DIR)" >&2; exit 2; }
    mkdir -p "$DIR"
    WARGS="--grpc=localhost:8080 --data_address=localhost:9090 \
--worker.number_of_buffers_in_global_buffer_manager=$NES_POOL_BUFFERS \
--worker.query_engine.number_of_worker_threads=$NES_THREADS \
--worker.default_max_inflight_buffers=$NES_MAX_INFLIGHT $NES_WORKER_ARGS"
    if [[ "${NES_EVENT_TRACE:-0}" == 1 ]]; then WARGS+=" --enable_event_trace=true"; fi
    CONTAINER=; SAMPLER_PID=
    if [[ "$NES_NATIVE" == 1 ]]; then
      PIN=(); if [[ -n "$DOCKER_CPUSET" ]]; then PIN=(taskset -c "$DOCKER_CPUSET"); fi
      # shellcheck disable=SC2086
      (cd "$DIR" && exec "${PIN[@]}" "$NES_WORKER_BIN" $WARGS > "$DIR/worker.log" 2>&1 < /dev/null) &
      WORKER_PID=$!
      "$BENCH_REPO/scripts/lib/sample.sh" "$WORKER_PID" "$DIR/resources.csv" "$NES_SAMPLE_MS" < /dev/null > /dev/null 2>&1 &
      SAMPLER_PID=$!
      disown -a
    else
      docker_args
      CONTAINER="nes-worker-$$-$RANDOM"
      rm -f "$DIR/worker.pid"
      docker run -d --name "$CONTAINER" "${DOCKER_ARGS[@]}" "$DOCKER_IMAGE" bash -c \
        "cd '$DIR' && echo \$\$ > '$DIR/worker.pid' && exec '$NES_WORKER_BIN' $WARGS > '$DIR/worker.log' 2>&1" > /dev/null
      for _ in $(seq 100); do [[ -s "$DIR/worker.pid" ]] && break; sleep 0.1; done
      WORKER_PID="$(cat "$DIR/worker.pid" 2>/dev/null || true)"
      [[ -n "$WORKER_PID" ]] || { echo "error: worker container did not start" >&2; docker rm -f "$CONTAINER" > /dev/null; exit 1; }
      rm -f "$DIR/sampler.pid"
      docker exec -d --user "$DOCKER_USER" "$CONTAINER" bash -c "echo \$\$ > '$DIR/sampler.pid'; exec bash \
'$BENCH_REPO/scripts/lib/sample.sh' $WORKER_PID '$DIR/resources.csv' $NES_SAMPLE_MS"
      for _ in $(seq 50); do [[ -s "$DIR/sampler.pid" ]] && break; sleep 0.1; done
      SAMPLER_PID="$(cat "$DIR/sampler.pid" 2>/dev/null || true)"
    fi
    printf 'CONTAINER=%s\nWORKER_PID=%s\nSAMPLER_PID=%s\n' "$CONTAINER" "$WORKER_PID" "$SAMPLER_PID" > "$ENVF"
    for _ in $(seq 600); do
      in_worker bash -c ': < /dev/tcp/localhost/8080' 2>/dev/null && exit 0
      alive || { echo "error: worker exited during startup, see $DIR/worker.log" >&2; "$0" stop "$DIR" || true; exit 1; }
      sleep 0.1
    done
    echo "error: worker not listening on 8080 after 60 s" >&2
    "$0" stop "$DIR" || true
    exit 1
    ;;
  stop)
    load
    if alive; then
      if [[ -n "$SAMPLER_PID" ]]; then in_worker kill "$SAMPLER_PID" 2>/dev/null || true; fi
      in_worker bash "$BENCH_REPO/scripts/lib/sample.sh" "$WORKER_PID" "$DIR/resources.csv" 0 || true
      in_worker grep VmHWM "/proc/$WORKER_PID/status" > "$DIR/worker_vmhwm" 2>/dev/null || true
      { in_worker cat "/proc/$WORKER_PID/stat"; echo "clk_tck $(in_worker getconf CLK_TCK)"; } > "$DIR/worker_stat" 2>/dev/null || true
    fi
    if [[ -z "$CONTAINER" ]]; then
      kill "$SAMPLER_PID" 2>/dev/null || true
      kill "$WORKER_PID" 2>/dev/null || true
      for _ in $(seq 100); do alive || break; sleep 0.1; done
      if alive; then kill -9 "$WORKER_PID" 2>/dev/null || true; fi
    else
      if [[ -n "${HOST_IDS:-}" ]]; then in_worker chown -R "$HOST_IDS" "$DIR" || true; fi
      docker stop -t 10 "$CONTAINER" > /dev/null 2>&1 || true
      docker rm -f "$CONTAINER" > /dev/null 2>&1 || true
    fi
    rm -f "$ENVF" "$DIR/worker.pid" "$DIR/sampler.pid"
    ;;
  exec) load; in_worker "$@" ;;
  alive) load; alive ;;
  pid) load; echo "$WORKER_PID" ;;
  *) sed -n '2,9p' "$0"; exit 2 ;;
esac
