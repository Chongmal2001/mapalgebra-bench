#!/usr/bin/env bash
# Configures and builds nes-single-node-worker and nes-cli in the NES development image.
#   scripts/nes/build.sh [extra cmake --build args]
set -euo pipefail
source "$(dirname "$0")/common.sh"
[[ "$NES_NATIVE" != 1 ]] || { echo "error: NES_NATIVE=1; build on a Docker machine, then scripts/nes/bundle.sh" >&2; exit 2; }

docker_run bash -c "
  set -e
  [[ -f '$NES_BUILD/CMakeCache.txt' ]] || cmake -S '$NES_REPO' -B '$NES_BUILD' -G Ninja -DCMAKE_BUILD_TYPE=$NES_BUILD_TYPE $NES_CMAKE_ARGS
  cmake --build '$NES_BUILD' --target nes-single-node-worker nes-cli -j $NES_JOBS $*
"
for b in "$NES_WORKER_BIN" "$NES_CLI_BIN"; do
  [[ -x "$b" ]] || { echo "error: $b not built (set NES_WORKER_BIN / NES_CLI_BIN in bench.env)" >&2; exit 1; }
done
echo "built: $NES_WORKER_BIN, $NES_CLI_BIN ($(git -C "$NES_REPO" rev-parse --short HEAD))"
