# Sourced by the scripts: settings from bench.env / bench.local.env and the NES docker run prefix (native: on the host).
BENCH_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_ENV="${BENCH_ENV:-$BENCH_REPO/bench.env}"
[[ -f "$BENCH_ENV" ]] || { echo "error: $BENCH_ENV missing" >&2; exit 2; }
# shellcheck disable=SC1091
if [[ -f "$BENCH_REPO/bench.local.env" ]]; then source "$BENCH_REPO/bench.local.env"; fi
# shellcheck source=../../bench.env
source "$BENCH_ENV"
DATA="$(realpath -m "$DATA")"
RESULTS="$(realpath -m "$RESULTS")"
export DATA RESULTS RUN_LABEL

docker_args() {  # options of docker run for NES (mounts, limits, user) -> DOCKER_ARGS
  DOCKER_ARGS=(--init --user "$DOCKER_USER" -e HOME=/tmp -e XDG_STATE_HOME=/tmp/state --workdir "$NES_REPO")
  local d
  mkdir -p "$RESULTS"
  for d in "$NES_REPO" "$BENCH_REPO" "$DATA" "$RESULTS"; do
    if [[ -d "$d" ]]; then DOCKER_ARGS+=(-v "$(realpath "$d"):$(realpath "$d")"); fi
  done
  for d in $DOCKER_EXTRA_MOUNTS; do DOCKER_ARGS+=(-v "$d"); done
  if [[ -n "$DOCKER_MEMORY" ]]; then DOCKER_ARGS+=(--memory "$DOCKER_MEMORY"); fi
  if [[ -n "$DOCKER_MEMORY_SWAP" ]]; then DOCKER_ARGS+=(--memory-swap "$DOCKER_MEMORY_SWAP"); fi
  if [[ -n "$DOCKER_CPUSET" ]]; then DOCKER_ARGS+=(--cpuset-cpus "$DOCKER_CPUSET"); fi
  for d in ${DOCKER_RUN_ARGS:-}; do DOCKER_ARGS+=("$d"); done
  if [[ -n "${NES_FOCAL_SUMMATION:-}" ]]; then DOCKER_ARGS+=(-e "NES_FOCAL_SUMMATION=$NES_FOCAL_SUMMATION"); fi
}

docker_run() {
  if [[ "$NES_NATIVE" == 1 ]]; then
    if [[ -n "$DOCKER_CPUSET" ]]; then taskset -c "$DOCKER_CPUSET" "$@"; else "$@"; fi
    return
  fi
  docker_args
  docker run --rm -i "${DOCKER_ARGS[@]}" "$DOCKER_IMAGE" "$@"
}

s2_store() {  # frames tile -> store path
  local s="$DATA/stores/$S2_NAME"
  if [[ "$1" != 0 ]]; then s+="-$1"; fi
  if [[ "$2" != 256 ]]; then s+="-t$2"; fi
  echo "$s"
}

provenance() {  # script [store...]: $RESULTS/provenance/<utc>-<script>.{json,env,diff}; finish time and exit status on exit
  local d="$RESULTS/provenance" stamp v
  stamp="$(date -u +%Y%m%dT%H%M%SZ)-$(basename "$1" .sh)"
  mkdir -p "$d"
  PROV_JSON="$d/$stamp.json"
  for v in $(compgen -v | grep -E '^(NES_|DOCKER_|GT_|S2_|PERF_|WINDOW|WARM_|HALO_|REF_|KEEP_)' || true) \
           DATA RESULTS RUN_LABEL REPS SYSTEMS CASES GRIDS FEATURES FILTER CASES_FILE GRIDS_FILE STORES_CONF; do
    printf '%s=%s\n' "$v" "${!v-}"
  done | sort -u > "$d/$stamp.env"
  if [[ -n "$(git -C "$BENCH_REPO" status --porcelain --untracked-files=no 2>/dev/null)" ]]; then
    git -C "$BENCH_REPO" diff HEAD > "$d/$stamp.diff"
  fi
  python "$BENCH_REPO/scripts/lib/provenance.py" start "$PROV_JSON" "$BENCH_REPO" "$NES_REPO" "$NES_NATIVE" \
    "$NES_BUNDLE" "$DOCKER_IMAGE" "$@"
  trap 'python "$BENCH_REPO/scripts/lib/provenance.py" end "$PROV_JSON" $?' EXIT
}
