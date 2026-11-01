#!/usr/bin/env bash
# Performance end to end: the grids of scripts/perf/grids.txt (stores built on demand), then the report.
#   scripts/performance.sh [G...]                default: GRIDS in bench.env
#   RUN_LABEL=try REPS=1 FILTER="op=max radius=4" scripts/performance.sh G1
# Results and provenance: $RESULTS (data/results/<RUN_LABEL>, default label: host name). Finished runs are skipped,
# so further grids can be added to the same result set later.
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
IDS="${*:-$GRIDS}"
provenance "$0" "$("$BENCH_REPO/scripts/data/stores.sh" --path perf)"
status=0
"$BENCH_REPO/scripts/perf/grid.sh" $IDS || status=1
"$BENCH_REPO/scripts/report.sh"
exit $status
