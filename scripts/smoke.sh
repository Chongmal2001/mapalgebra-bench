#!/usr/bin/env bash
# Smoke test of the whole pipeline in a few minutes, before real runs: tiny synthetic stores (scripts/data/smoke-stores.sh),
# correctness cases (oracle and GeoTrellis references, NES), one perf run per system, report. No Sentinel-2 data needed.
#   scripts/smoke.sh                  results and provenance: data/results/smoke-<host> (rerun: scripts/clean.sh --results smoke-<host>)
set -euo pipefail
export RUN_LABEL="${RUN_LABEL:-smoke-$(hostname -s 2>/dev/null || hostname)}"
source "$(dirname "$0")/lib/common.sh"
"$BENCH_REPO/scripts/data/smoke-stores.sh"
S="$DATA/stores"
provenance "$0" "$S/smoke-1b" "$S/smoke-2b" "$S/smoke-rgtt"
CASE="$BENCH_REPO/scripts/correctness/case.sh"
export CASE_ID=smoke
status=0
step() { echo "########## $*"; "$@" || { echo "FAILED: $*" >&2; status=1; }; }
step env REF_SYSTEM=oracle "$CASE" "$S/smoke-1b" focal:sum:square:1                        # basic, 3 frames
step env REF_SYSTEM=oracle "$CASE" "$S/smoke-2b" focal:mean:circle:2                       # 2 bands, NoData, padded edges
step env REF_SYSTEM=oracle "$CASE" "$S/smoke-rgtt" focal:sum:square:5                      # r > tile size
step env REF_SYSTEM=oracle NES_FOCAL_SUMMATION=kahan WINDOW_FRAMES=2 "$CASE" "$S/smoke-2b" focal:mean:square:1
step "$CASE" "$S/smoke-1b" focal:max:square:1                                              # GeoTrellis full-grid reference
step env REPS=1 "$BENCH_REPO/scripts/perf/run.sh" "$S/smoke-1b" focal:max:square:1         # NES and GeoTrellis perf
step env REPS=1 SYSTEMS=nes-passthrough "$BENCH_REPO/scripts/perf/run.sh" "$S/smoke-1b" focal:max:square:1
step "$BENCH_REPO/scripts/report.sh"
python - "$RESULTS/correctness" <<'PY'
import json, pathlib, sys
rs = sorted(pathlib.Path(sys.argv[1]).glob("smoke/*/*.json"))
for p in rs:
    r = json.loads(p.read_text())
    print(f"{p.parent.name + '/' + p.stem:<40}{'PASS' if r['passed'] else 'FAIL'}")
print(f"{sum(json.loads(p.read_text())['passed'] for p in rs)}/{len(rs)} PASS")
PY
echo "smoke $([[ $status == 0 ]] && echo OK || echo FAILED): $RESULTS"
exit $status
