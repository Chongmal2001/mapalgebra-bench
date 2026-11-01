#!/usr/bin/env bash
# Warm input: reads a store's frame files once so they sit in the page cache and the next run reads from RAM.
# Warns when the store does not fit into available memory (the cache cannot hold it, runs read from disk).
#   scripts/lib/warm.sh STORE
set -euo pipefail
S="$1"
bytes=$(du -sb "$S/frames" | cut -f1)
avail=$(( $(sed -n 's/^MemAvailable: *\([0-9]*\) kB/\1/p' /proc/meminfo) * 1024 ))
if (( bytes > avail / 2 )); then
  echo "warning: store $(basename "$S") is $((bytes >> 20)) MiB, available memory $((avail >> 20)) MiB; input may not stay cached" >&2
fi
cat "$S"/frames/*.mpl > /dev/null
