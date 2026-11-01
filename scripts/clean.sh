#!/usr/bin/env bash
# Removes what can be regenerated from the raw data: stores, references, imported outputs (and the pre-RUN_LABEL
# directories runs, perf, profile). Result sets in data/results stay unless named.
#   scripts/clean.sh                  caches only
#   scripts/clean.sh --results LABEL  also data/results/LABEL (repeatable; --results all: every result set)
#   scripts/clean.sh --raw            also the downloaded scenes
set -euo pipefail
source "$(dirname "$0")/lib/common.sh"
dirs=(stores refs out runs perf profile)
while [[ $# -gt 0 ]]; do
  case "$1" in
    --raw) dirs+=(raw); shift ;;
    --results) [[ "${2:-}" == all ]] && dirs+=(results) || dirs+=("results/${2:?label}"); shift 2 ;;
    *) sed -n '2,6p' "$0"; exit 2 ;;
  esac
done
for d in "${dirs[@]}"; do
  if [[ -e "$DATA/$d" ]]; then echo "removing $DATA/$d"; rm -rf "${DATA:?}/$d"; fi
done
echo "clean"
