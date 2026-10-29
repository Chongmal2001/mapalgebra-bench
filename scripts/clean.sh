#!/usr/bin/env bash
# Removes everything derived from the raw data: stores, references, NES runs and imports, perf runs, results.
#   scripts/clean.sh          keeps the downloaded scenes in data/raw
#   scripts/clean.sh --raw    removes data/raw as well
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
DATA="$(realpath -m "${DATA:-$REPO/data}")"
dirs=(stores refs out runs perf profile results)
if [[ "${1:-}" == --raw ]]; then dirs+=(raw); fi
for d in "${dirs[@]}"; do
  if [[ -e "$DATA/$d" ]]; then echo "removing $DATA/$d"; rm -rf "${DATA:?}/$d"; fi
done
echo "clean"
