#!/bin/sh
# Compatibility entry point. Commit/review/push are explicit Git operations, not build side effects.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec "$ROOT/standalone/build/build.sh" "$@"
