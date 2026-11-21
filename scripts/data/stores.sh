#!/usr/bin/env bash
# Builds the named stores of scripts/data/stores.txt (and the stores they derive from); existing stores are kept.
# S2 scenes are fetched first if none are there; scenes that are NoData everywhere are skipped, an S2 store takes the
# first N of the rest in date order.
#   scripts/data/stores.sh [NAME...]       default: main halo perf
#   scripts/data/stores.sh --path NAME     prints the store's directory (does not build it)
#   scripts/data/stores.sh --list          names and recipes
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
CONF="${STORES_CONF:-$BENCH_REPO/scripts/data/stores.txt}"
RAW="$DATA/raw/$S2_NAME"
PERIOD_MS=432000000  # 5 days, the S2 revisit

recipe() {  # name -> recipe words in REC (variables expanded)
  local line
  line="$(awk -v n="$1" '$1 == n { $1 = ""; print; exit }' "$CONF")"
  [[ -n "$line" ]] || { echo "error: no store '$1' in $CONF" >&2; exit 2; }
  set -f
  eval "REC=($line)"
  set +f
}
root() {  # name -> recipe kind at the root of its derivation chain
  recipe "$1"
  if [[ "${REC[0]}" == derive ]]; then root "${REC[1]}"; else echo "${REC[0]}"; fi
}
path_of() {
  recipe "$1"
  case "${REC[0]}" in
    s2) s2_store "${REC[1]}" "${REC[2]}" ;;
    derive) if [[ "$(root "$1")" == s2 ]]; then echo "$DATA/stores/$S2_NAME-$1"; else echo "$DATA/stores/$1"; fi ;;
    synth) echo "$DATA/stores/$1" ;;
    *) echo "error: unknown recipe '${REC[0]}' for store $1" >&2; exit 2 ;;
  esac
}
scenes() {
  if [[ -n "${SCENES_LOADED:-}" ]]; then return; fi
  if ! compgen -G "$RAW/*.tif" > /dev/null; then "$BENCH_REPO/scripts/data/fetch-s2.sh"; fi
  mapfile -t SCENES < <(python - "$RAW"/*.tif <<'PY'
import sys, rasterio
for p in sorted(a for a in sys.argv[1:] if not a.endswith('.tmp.tif')):
    with rasterio.open(p) as ds:
        if ds.dataset_mask().any():
            print(p)
        else:
            print(f"skipping {p}: NoData everywhere", file=sys.stderr)
PY
)
  SCENES_LOADED=1
}
build() {
  local name="$1" out n
  out="$(path_of "$name")"
  if [[ -e "$out/manifest.json" ]]; then echo "== $name: $(basename "$out") exists"; return; fi
  recipe "$name"
  local r=("${REC[@]}")
  case "${r[0]}" in
    s2)
      scenes
      n="${r[1]}"; if [[ "$n" == 0 ]]; then n=${#SCENES[@]}; fi
      (( n <= ${#SCENES[@]} )) || { echo "error: $name needs $n scenes, $RAW has ${#SCENES[@]}" >&2; exit 1; }
      echo "== $name: $(basename "$out"), $n scene(s), tile size ${r[2]}"
      mabench prepare "${SCENES[@]:0:$n}" --out "$out" --tile-size "${r[2]}" --frame-period-ms "$PERIOD_MS" --force ;;
    derive)
      build "${r[1]}"
      echo "== $name: $(basename "$out") <- ${r[1]} ${r[*]:2}"
      mabench derive "$(path_of "${r[1]}")" --out "$out" "${r[@]:2}" --force ;;
    synth)
      echo "== $name: $(basename "$out") (synthetic)"
      mabench synth --out "$out" "${r[@]:1}" --force ;;
  esac
}

case "${1:-}" in
  --path) path_of "$2" ;;
  --list) grep -v '^\s*#' "$CONF" | grep -v '^\s*$' ;;
  *) if [[ $# -eq 0 ]]; then set -- main halo perf; fi
     for s in "$@"; do build "$s"; done ;;
esac
