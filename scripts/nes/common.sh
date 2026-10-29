# Sourced by scripts/nes/*.sh: settings from bench.env and the docker run prefix (native: runs on the host).
BENCH_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_ENV="${BENCH_ENV:-$BENCH_REPO/bench.env}"
[[ -f "$BENCH_ENV" ]] || { echo "error: $BENCH_ENV missing" >&2; exit 2; }
# shellcheck disable=SC1091
if [[ -f "$BENCH_REPO/bench.local.env" ]]; then source "$BENCH_REPO/bench.local.env"; fi
# shellcheck source=../../bench.env
source "$BENCH_ENV"

docker_run() {
  if [[ "$NES_NATIVE" == 1 ]]; then
    if [[ -n "$DOCKER_CPUSET" ]]; then taskset -c "$DOCKER_CPUSET" "$@"; else "$@"; fi
    return
  fi
  local args=(run --rm -i --init --user "$DOCKER_USER" -e HOME=/tmp -e XDG_STATE_HOME=/tmp/state --workdir "$NES_REPO")
  local d
  for d in "$NES_REPO" "$BENCH_REPO" "$DATA"; do
    if [[ -d "$d" ]]; then args+=(-v "$(realpath "$d"):$(realpath "$d")"); fi
  done
  for d in $DOCKER_EXTRA_MOUNTS; do args+=(-v "$d"); done
  if [[ -n "$DOCKER_MEMORY" ]]; then args+=(--memory "$DOCKER_MEMORY"); fi
  if [[ -n "$DOCKER_MEMORY_SWAP" ]]; then args+=(--memory-swap "$DOCKER_MEMORY_SWAP"); fi
  if [[ -n "$DOCKER_CPUSET" ]]; then args+=(--cpuset-cpus "$DOCKER_CPUSET"); fi
  for d in ${DOCKER_RUN_ARGS:-}; do args+=("$d"); done
  if [[ -n "${NES_FOCAL_SUMMATION:-}" ]]; then args+=(-e "NES_FOCAL_SUMMATION=$NES_FOCAL_SUMMATION"); fi
  docker "${args[@]}" "$DOCKER_IMAGE" "$@"
}
