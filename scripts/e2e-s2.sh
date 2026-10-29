#!/usr/bin/env bash
# Sentinel-2 end to end: fetch (if missing) -> tile store -> per workload: GeoTrellis reference, NES, compare -> summary.
#
#   scripts/e2e-s2.sh
#   CLEAN=1 S2_FRAMES=3 WORKLOADS="focal:mean:square:3" scripts/e2e-s2.sh
#
# CLEAN=1     run scripts/clean.sh first (keeps the downloaded scenes; CLEAN=raw downloads them again)
# S2_FRAMES   number of scenes to use, in date order (default 0 = all); the store is named s2-berlin or s2-berlin-<N>
# TILE_SIZE   tile size of the store (default 256)
# WORKLOADS   space-separated workloads (default: mean and max, square, r = 3)
# Needs: NES built (scripts/nes/build.sh), sbt, mabench; scripts/fetch-s2.sh needs curl, jq and GDAL.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
export DATA
RAW="$DATA/raw/s2-berlin"
N="${S2_FRAMES:-0}"
T="${TILE_SIZE:-256}"
WORKLOADS="${WORKLOADS:-focal:mean:square:3 focal:max:square:3}"
STORE="$DATA/stores/s2-berlin"
if [[ "$N" != 0 ]]; then STORE+="-$N"; fi
if [[ "$T" != 256 ]]; then STORE+="-t$T"; fi

case "${CLEAN:-0}" in
  1) scripts/clean.sh ;;
  raw) scripts/clean.sh --raw ;;
esac

if ! compgen -G "$RAW/*.tif" > /dev/null; then
  echo "== fetching S2 scenes -> $RAW"
  (cd "$DATA/.." && "$REPO/scripts/fetch-s2.sh")
fi

if [[ ! -e "$STORE/manifest.json" ]]; then
  mapfile -t scenes < <(ls "$RAW"/*.tif | sort)
  if [[ "$N" != 0 ]]; then scenes=("${scenes[@]:0:$N}"); fi
  echo "== tile store ${#scenes[@]} scene(s), tile size $T -> $STORE"
  mabench prepare "${scenes[@]}" --out "$STORE" --tile-size "$T" --frame-period-ms 432000000 --force
fi

status=0
for w in $WORKLOADS; do
  echo "=================== $w"
  scripts/run-focal.sh "$STORE" "$w" || status=1
done

echo "=================== summary ($(basename "$STORE"))"
python - "$DATA/results/$(basename "$STORE")" $WORKLOADS <<'EOF'
import json, sys, os
res, workloads = sys.argv[1], sys.argv[2:]
print(f"{'workload':<24}{'verdict':>8}{'cells':>14}{'exact':>10}{'max ULP':>10}  coverage")
for w in workloads:
    p = os.path.join(res, w.removeprefix("focal:").replace(":", "-") + ".json")
    if not os.path.exists(p):
        print(f"{w:<24}{'missing':>8}"); continue
    r = json.load(open(p)); t = r["totals"]["all"]
    print(f"{w:<24}{'PASS' if r['passed'] else 'FAIL':>8}{t['cells']:>14}{t['exact_rate']:>10.4%}{t['max_ulp']:>10.3g}  "
          f"{'; '.join(r['coverage_problems']) or 'ok'}")
EOF
exit $status
