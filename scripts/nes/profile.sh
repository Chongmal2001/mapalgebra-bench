#!/usr/bin/env bash
# Profiles one nes-cli topology: runs it like scripts/nes/run-query.sh, but as root in a privileged container that has
# perf, and samples the worker for SECONDS (default 60) from 3 s after query start. Docker Desktop runs containers in a
# VM, so perf cannot see the worker from the host. The image with perf is built once on top of DOCKER_IMAGE.
#   scripts/nes/profile.sh TOPOLOGY.yaml OUT_DIR [SECONDS]
# OUT_DIR gets the run-query.sh files plus perf.data, perf.log and perf-report.txt (own time per function).
# Worker threads: PERF_THREADS (default 1, as in scripts/run-perf.sh).
set -euo pipefail
source "$(dirname "$0")/common.sh"
[[ "$NES_NATIVE" != 1 ]] || { echo "error: profile.sh needs Docker (NES_NATIVE=1)" >&2; exit 2; }
[[ $# -ge 2 ]] || { sed -n '2,7p' "$0"; exit 2; }
PROFILE_IMAGE="${PROFILE_IMAGE:-${DOCKER_IMAGE%:*}:perf}"
if ! docker image inspect "$PROFILE_IMAGE" > /dev/null 2>&1; then
  echo "== building $PROFILE_IMAGE (perf on top of $DOCKER_IMAGE)"
  docker build -t "$PROFILE_IMAGE" - <<EOF
FROM $DOCKER_IMAGE
USER root
RUN apt-get update && apt-get install -y --no-install-recommends linux-tools-generic && rm -rf /var/lib/apt/lists/*
EOF
fi
OUT="$(realpath -m "$2")"
DOCKER_IMAGE="$PROFILE_IMAGE" DOCKER_USER=0:0 DOCKER_RUN_ARGS="--privileged" HOST_IDS="$(id -u):$(id -g)" \
  NES_THREADS="${PERF_THREADS:-1}" NES_PROFILE_SECS="${3:-60}" "$(dirname "$0")/run-query.sh" "$1" "$OUT" || true
[[ -s "$OUT/perf-report.txt" ]] || { echo "error: no profile; see $OUT/perf.log" >&2; exit 1; }
grep -E '^ +[0-9.]+%' "$OUT/perf-report.txt" | head -n 40
echo "full report: $OUT/perf-report.txt"
