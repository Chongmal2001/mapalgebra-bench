#!/usr/bin/env bash
# Packs the built NES worker and CLI with every shared library they load (glibc and its loader included) into one folder
# that runs on any x86-64 Linux without Docker or root: NES_NATIVE=1 and NES_BUNDLE=<folder> in bench.local.env there.
#   scripts/nes/bundle.sh OUT_DIR        e.g. scripts/nes/bundle.sh ~/nes-bundle
# Takes the binaries of NES_BUILD (scripts/nes/build.sh) from the image DOCKER_IMAGE they were built in, then checks
# that the bundle starts a worker in a plain ubuntu container (nothing from the build visible).
set -euo pipefail
source "$(dirname "$0")/common.sh"
[[ $# -eq 1 ]] || { sed -n '2,6p' "$0"; exit 2; }
[[ "$NES_NATIVE" != 1 ]] || { echo "error: run on the build machine without NES_NATIVE=1" >&2; exit 2; }
OUT="$(realpath -m "$1")"
[[ -x "$NES_WORKER_BIN" && -x "$NES_CLI_BIN" ]] || { echo "error: NES binaries missing, run scripts/nes/build.sh" >&2; exit 2; }

CACHE="$NES_BUILD/CMakeCache.txt"
TYPE="$(sed -n 's/^CMAKE_BUILD_TYPE:STRING=//p' "$CACHE")"
SAN="$(sed -n 's/^USE_SANITIZER:STRING=//p' "$CACHE")"
TRIPLET="$(sed -n 's/^VCPKG_TARGET_TRIPLET:STRING=//p' "$CACHE")"
[[ "$TYPE" == Release ]] || echo "warning: build type '$TYPE', not Release" >&2
if [[ "${SAN,,}" != none || "$TRIPLET" =~ address|thread|undefined ]]; then
  echo "warning: sanitizer build (USE_SANITIZER=$SAN, triplet $TRIPLET); use an image from" \
       "install-local-docker-environment.sh --no-sanitizer for timing" >&2
fi

rm -rf "$OUT"; mkdir -p "$OUT/bin" "$OUT/lib"
DOCKER_EXTRA_MOUNTS+=" $OUT:$OUT"
docker_run bash -s <<EOF
set -euo pipefail
cp '$NES_WORKER_BIN' '$NES_CLI_BIN' '$OUT/bin/'
deps="\$(ldd '$OUT/bin/'*)"
if grep -q 'not found' <<< "\$deps"; then grep 'not found' <<< "\$deps" >&2; exit 1; fi
awk '/=> \\// { print \$3 }' <<< "\$deps" | sort -u | xargs -r cp -L -t '$OUT/lib/'
cp -L /lib64/ld-linux-x86-64.so.2 '$OUT/lib/'
gcc_s="\$(ldconfig -p | awk '/libgcc_s.so.1 .*x86-64/ { print \$NF; exit }')"   # dlopen'ed by glibc for unwinding
if [[ -n "\$gcc_s" && ! -e '$OUT/lib/libgcc_s.so.1' ]]; then cp -L "\$gcc_s" '$OUT/lib/'; fi
echo "BUNDLE_IMAGE_OS='\$(. /etc/os-release && echo "\$PRETTY_NAME")'" > '$OUT/BUILD_INFO'
EOF

for b in nes-single-node-worker nes-cli; do
  cat > "$OUT/$b" <<EOF
#!/bin/sh
D="\$(cd "\$(dirname "\$0")" && pwd)"
exec "\$D/lib/ld-linux-x86-64.so.2" --library-path "\$D/lib" "\$D/bin/$b" "\$@"
EOF
  chmod +x "$OUT/$b"
done
cp "$CACHE" "$OUT/"
cat >> "$OUT/BUILD_INFO" <<EOF
BUNDLE_NES_COMMIT=$(git -C "$NES_REPO" rev-parse HEAD)
BUNDLE_NES_DIRTY=$( [[ -n "$(git -C "$NES_REPO" status --porcelain --untracked-files=no)" ]] && echo true || echo false)
BUNDLE_IMAGE=$(docker image inspect --format '{{.Id}}' "$DOCKER_IMAGE")
BUNDLE_BUILD_TYPE=$TYPE
BUNDLE_SANITIZER=$SAN
BUNDLE_TRIPLET=$TRIPLET
BUNDLE_CREATED_UTC=$(date -u +%FT%TZ)
EOF

echo "== self-test in ubuntu:24.04"
docker run --rm -i --user "$(id -u):$(id -g)" -e HOME=/tmp -w /tmp -v "$OUT:/b:ro" ubuntu:24.04 bash -s <<'EOF'
/b/nes-single-node-worker --grpc=localhost:8080 --data_address=localhost:9090 > /tmp/w.log 2>&1 &
W=$!
for i in $(seq 300); do
  (: < /dev/tcp/localhost/8080) 2>/dev/null && { echo "worker up"; kill $W; exit 0; }
  kill -0 $W 2>/dev/null || break
  sleep 0.1
done
echo "self-test failed, worker log:" >&2; cat /tmp/w.log >&2; exit 1
EOF
du -sh "$OUT"
echo "bundle: $OUT ($(sed -n 's/^BUNDLE_NES_COMMIT=//p' "$OUT/BUILD_INFO" | cut -c1-10))"
