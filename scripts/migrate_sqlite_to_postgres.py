"""One-time importer for the previous MAI SQLite schema.

Usage inside the running API container:
    python scripts/migrate_sqlite_to_postgres.py /tmp/legacy_mai_corrosion.db

The source database is opened read-only. Batches/history are idempotent; analysis IDs
use PostgreSQL ON CONFLICT DO NOTHING so a rerun does not duplicate audit rows.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.schemas import FailureHistoryItem, TelemetryPoint, WorkHistoryItem


def _parse_json(value):
    if value is None:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _as_utc(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def migrate(source_path: Path) -> dict[str, int]:
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite backup not found: {source_path}")
    db.init_db()
    counts = {"telemetry_batches": 0, "telemetry_rows": 0, "history_batches": 0,
              "history_rows": 0, "analysis_runs": 0}
    source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        if "ingestion_batches" in tables and "telemetry" in tables:
            batches = source.execute("SELECT batch_id, well_id FROM ingestion_batches ORDER BY created_at, batch_id").fetchall()
            for batch in batches:
                rows = source.execute(
                    "SELECT payload_json FROM telemetry WHERE batch_id=? ORDER BY record_index",
                    (batch["batch_id"],),
                ).fetchall()
                records = [TelemetryPoint.model_validate(_parse_json(row["payload_json"])) for row in rows]
                if records:
                    result = db.save_batch(batch["well_id"], batch["batch_id"], records)
                    counts["telemetry_batches"] += not result["idempotent_replay"]
                    counts["telemetry_rows"] += result["records_stored"]

        if "history_batches" in tables and "history_events" in tables:
            batches = source.execute("SELECT batch_id, well_id FROM history_batches ORDER BY created_at, batch_id").fetchall()
            for batch in batches:
                rows = source.execute(
                    "SELECT event_type, payload_json FROM history_events WHERE batch_id=? ORDER BY event_date, id",
                    (batch["batch_id"],),
                ).fetchall()
                work: list[WorkHistoryItem] = []
                failures: list[FailureHistoryItem] = []
                for row in rows:
                    payload = _parse_json(row["payload_json"])
                    if row["event_type"] == "work":
                        work.append(WorkHistoryItem.model_validate(payload))
                    elif row["event_type"] == "failure":
                        failures.append(FailureHistoryItem.model_validate(payload))
                if work or failures:
                    result = db.save_history(batch["well_id"], batch["batch_id"], work, failures)
                    counts["history_batches"] += not result["idempotent_replay"]
                    counts["history_rows"] += result["work_records_stored"] + result["failure_records_stored"]

        if "analysis_runs" in tables:
            columns = {row["name"] for row in source.execute("PRAGMA table_info(analysis_runs)").fetchall()}
            for row in source.execute("SELECT * FROM analysis_runs ORDER BY created_at, analysis_id").fetchall():
                result_json = _parse_json(row["result_json"])
                if not isinstance(result_json, dict):
                    continue
                model_versions = _parse_json(row["model_versions_json"]) or {}
                values = {
                    "analysis_id": row["analysis_id"], "well_id": row["well_id"],
                    "request_id": row["request_id"] if "request_id" in columns else None,
                    "created_at": _as_utc(row["created_at"] if "created_at" in columns else None),
                    "status": row["status"] if "status" in columns else result_json.get("status", "migrated"),
                    "model_versions_json": model_versions,
                    "result_json": result_json,
                    "agent_status": row["agent_status"] if "agent_status" in columns else None,
                    "agent_updated_at": _as_utc(row["agent_updated_at"])
                    if "agent_updated_at" in columns and row["agent_updated_at"] else None,
                    "agent_response_json": _parse_json(row["agent_response_json"])
                    if "agent_response_json" in columns else None,
                    "external_data_json": _parse_json(row["external_data_json"])
                    if "external_data_json" in columns else None,
                }
                statement = pg_insert(db.analysis_runs).values(**values).on_conflict_do_nothing(
                    index_elements=[db.analysis_runs.c.analysis_id])
                with db.engine().begin() as conn:
                    inserted = conn.execute(statement)
                counts["analysis_runs"] += max(0, inserted.rowcount or 0)
    finally:
        source.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="Copy a previous MAI SQLite database into configured PostgreSQL.")
    parser.add_argument("sqlite_path", type=Path, help="Path to a stopped/consistent SQLite backup")
    args = parser.parse_args()
    counts = migrate(args.sqlite_path)
    print("Migration completed:")
    for name, count in counts.items():
        print(f"  {name}: {count}")
    print("SQLite source was opened read-only and was not modified.")


if __name__ == "__main__":
    main()
