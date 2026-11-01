#!/usr/bin/env bash
# Correctness end to end: the cases of scripts/correctness/cases.txt (stores built on demand), then the report.
#   scripts/correctness.sh [K...]                default: CASES in bench.env
#   RUN_LABEL=try FILTER="op=mean radius=1,4" scripts/correctness.sh K1
# Results and provenance: $RESULTS (data/results/<RUN_LABEL>, default label: host name). Finished runs are skipped,
# so further cases can be added to the same result set later.
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
IDS="${*:-$CASES}"
S="$BENCH_REPO/scripts/data/stores.sh"
provenance "$0" "$("$S" --path main)" "$("$S" --path halo)"
status=0
"$BENCH_REPO/scripts/correctness/suite.sh" $IDS || status=1
"$BENCH_REPO/scripts/report.sh"
exit $status
