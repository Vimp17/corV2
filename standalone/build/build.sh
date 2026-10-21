#!/bin/sh
# Build the Windows archive of the standalone demo build on a Linux machine with Docker.
#
#   standalone/build/build.sh            build standalone/dist/mai-demo-win64-<date>-<commit>.zip
#   standalone/build/build.sh --relock   resolve the Windows packages again (see README-DEMO.md)
#
# The build runs in a Python 3.12 container (MAI_BUILD_IMAGE, default python:3.12-slim) with the
# host network, so HTTP(S)_PROXY of this machine is used for the downloads. Downloads are cached in
# standalone/dist/cache. Nothing is installed on this machine.
set -eu
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=${MAI_BUILD_IMAGE:-python:3.12-slim}
COMMIT=$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)
BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no 2>/dev/null)" ]; then DIRTY=yes; else DIRTY=no; fi
PROXY=${HTTPS_PROXY:-${https_proxy:-}}
mkdir -p "$ROOT/standalone/dist"
# The lock is mounted writable for --relock; it must exist as a file before docker mounts it.
[ -f "$ROOT/standalone/requirements-windows.lock" ] || : > "$ROOT/standalone/requirements-windows.lock"
exec docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_HTTP_TIMEOUT=600 -e UV_HTTP_RETRIES=10 -e UV_CONCURRENT_DOWNLOADS=2 \
  -e HTTPS_PROXY="$PROXY" -e HTTP_PROXY="${HTTP_PROXY:-${http_proxy:-$PROXY}}" \
  -e https_proxy="$PROXY" -e http_proxy="${HTTP_PROXY:-${http_proxy:-$PROXY}}" -e NO_PROXY="${NO_PROXY:-}" \
  -e MAI_BUILD_SRC=/src -e MAI_BUILD_DIST=/dist \
  -e MAI_BUILD_GIT_COMMIT="$COMMIT" -e MAI_BUILD_GIT_BRANCH="$BRANCH" -e MAI_BUILD_GIT_DIRTY="$DIRTY" \
  -v "$ROOT:/src:ro" -v "$ROOT/standalone/dist:/dist" -v "$ROOT/standalone/requirements-windows.lock:/src/standalone/requirements-windows.lock" \
  --entrypoint python "$IMAGE" /src/standalone/build/build_windows.py "$@"
