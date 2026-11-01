#!/usr/bin/env bash
# Figures and tables of one result set: $RESULTS/perf -> perf.csv, then mabench report -> $RESULTS/report.
#   scripts/report.sh [RESULTS]          default: $RESULTS of bench.env (data/results/<RUN_LABEL>)
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
R="$(realpath "${1:-$RESULTS}")"
if [[ -d "$R/perf" ]]; then mabench perf-table "$R/perf" --out "$R/perf.csv"; fi
mabench report --perf "$R/perf.csv" --results "$R/correctness" --out "$R/report"
