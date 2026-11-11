#!/bin/sh
# Run from a user terminal when Codex's approval service is unavailable.
# Creates a feature commit, builds and verifies Windows ZIP, pushes the feature branch.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
# Optional: verify the full local dataset in the prepared test image. It is never staged.
if [ "$#" -gt 0 ]; then
    DATASET=$(realpath "$1")
    docker run --rm --network none -v "$ROOT:/src:ro" -v "$(dirname "$DATASET"):/co2-input:ro" \
        -w /src -e PYTHONDONTWRITEBYTECODE=1 corv2-test-mssql:latest \
        python scripts/verify_co2_dataset.py "/co2-input/$(basename "$DATASET")"
fi
BRANCH=codex/co2-model-demo
if [ "$(git branch --show-current)" != "$BRANCH" ]; then
    if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
        git switch "$BRANCH"
    else
        git switch -c "$BRANCH"
    fi
fi
# Explicit list: excludes .env, user compose override and the full dataset.
set -- \
    README.md app/api/routes/rating.py app/config.py app/core/excel_cells.py \
    app/core/proposal_agent.py app/core/proposal_language.py app/core/rating_export.py \
    app/db/schema.py app/db/sources.py app/main.py app/schemas.py dashboard/index.html \
    standalone/README.md standalone/build/build_windows.py standalone/env.template \
    .github/workflows/build-demo.yml app/api/routes/co2.py app/core/co2_corrosion.py \
    app/core/co2_import.py app/db/co2.py examples/co2/v4-example.json \
    scripts/check_demo_archive.py scripts/prepare_test_db.py scripts/smoke_demo.py \
    scripts/test_co2_local.sh scripts/verify_co2_dataset.py scripts/release_co2_demo.sh \
    tests/test_co2.py docs/co2-implementation.md docs/integration-plan.md
# A previous interrupted run may already have staged these same files.
# Refuse unrelated staged paths, but preserve and resume our own staged changes.
python3 - "$@" <<'PY_CHECK'
import subprocess
import sys
allowed = set(sys.argv[1:])
staged = subprocess.check_output(['git', 'diff', '--cached', '--name-only', '--no-renames', '-z'])
unknown = {p.decode() for p in staged.split(b'\0') if p} - allowed
if unknown:
    print('В индексе есть посторонние файлы; ничего не добавлено: ' + ', '.join(sorted(unknown)), file=sys.stderr)
    raise SystemExit(1)
PY_CHECK
git add -- "$@"
if ! git diff --cached --quiet; then
    git diff --cached --check
    git --no-pager diff --cached --stat
    git commit -m 'Add separate CO2 corrosion scenarios, v4 import and Windows demo CI'
fi
if ! git diff --quiet; then
    echo 'Остались изменения отслеживаемых файлов; сборка остановлена для проверки.' >&2
    exit 1
fi
standalone/build/build.sh
python3 scripts/check_demo_archive.py
git push -u origin "$BRANCH"
echo 'Локальная сборка проверена. Ветка отправлена. Дождитесь всех заданий GitHub Actions:'
echo 'https://github.com/Vimp17/corV2/actions'
