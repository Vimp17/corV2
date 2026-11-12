#!/bin/sh
# Isolated integration tests. No production settings or database are used.
set -eu
ROOT=$(cd "$(dirname "$0")/.." && pwd)
TEST_ID="corv2-co2-test-$$"
TEST_IMAGE=${MAI_TEST_IMAGE:-corv2-test-mssql:latest}
case ${MAI_TEST_SQL_DRIVER:-pymssql} in
    pymssql) TEST_URL='mssql+pymssql://sa:CI-only-CorV2-2026!@sqlserver:1433/mai_test' ;;
    pyodbc) TEST_URL='mssql+pyodbc://sa:CI-only-CorV2-2026!@sqlserver:1433/mai_test?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes' ;;
    *) echo 'MAI_TEST_SQL_DRIVER must be pymssql or pyodbc' >&2; exit 1 ;;
esac
cleanup() {
    docker rm -f "$TEST_ID" >/dev/null 2>&1 || true
    docker network rm "$TEST_ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM
docker network create "$TEST_ID" >/dev/null
docker run --rm -d --name "$TEST_ID" --network "$TEST_ID" --network-alias sqlserver \
    -e ACCEPT_EULA=Y -e MSSQL_SA_PASSWORD='CI-only-CorV2-2026!' \
    mcr.microsoft.com/mssql/server:2022-latest >/dev/null
docker run --rm --network "$TEST_ID" -v "$ROOT:/src:ro" -w /src \
    -e MAI_CI_SQL_HOST=sqlserver -e PYTHONDONTWRITEBYTECODE=1 \
    "$TEST_IMAGE" python scripts/prepare_test_db.py
docker run --rm --network "$TEST_ID" -v "$ROOT:/src:ro" -w /src \
    -e MAI_TEST_DATABASE_URL="$TEST_URL" \
    -e MAI_TEST_DATABASE_SCHEMA=co2_tests -e PYTHONDONTWRITEBYTECODE=1 \
    "$TEST_IMAGE" python -m pytest -q -p no:cacheprovider "$@"
