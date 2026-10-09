"""Baseline schema (as created by metadata.create_all up to commit 97b0bf0).

Idempotent so it can run both on an empty database and on a database created before
Alembic was introduced: tables are created only when missing, and the legacy
source_health primary key is rebuilt only when it does not already include `origin`.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("ingestion_batches"):
        op.create_table(
            "ingestion_batches",
            sa.Column("batch_id", sa.String(128), primary_key=True),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("record_count", sa.Integer, nullable=False),
            sa.Column("created_at", TZ, nullable=False),
            sa.Column("payload_sha256", sa.String(64), nullable=False),
        )
    if not _has_table("telemetry"):
        op.create_table(
            "telemetry",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.String(128),
                      sa.ForeignKey("ingestion_batches.batch_id", ondelete="CASCADE"), nullable=False),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("source_id", sa.String(128)),
            sa.Column("source_occurrence", sa.Integer),
            sa.Column("record_index", sa.Integer, nullable=False),
            sa.Column("timestamp", TZ, nullable=False),
            sa.Column("payload_json", sa.JSON, nullable=False),
            sa.UniqueConstraint("batch_id", "record_index", name="uq_telemetry_batch_record"),
            sa.UniqueConstraint("well_id", "source_id", "timestamp", "source_occurrence",
                                name="uq_telemetry_source_observation"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_telemetry_well_time ON telemetry (well_id, timestamp)")

    if not _has_table("history_batches"):
        op.create_table(
            "history_batches",
            sa.Column("batch_id", sa.String(128), primary_key=True),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("work_count", sa.Integer, nullable=False),
            sa.Column("failure_count", sa.Integer, nullable=False),
            sa.Column("payload_sha256", sa.String(64), nullable=False),
            sa.Column("created_at", TZ, nullable=False),
        )
    if not _has_table("history_events"):
        op.create_table(
            "history_events",
            sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
            sa.Column("batch_id", sa.String(128),
                      sa.ForeignKey("history_batches.batch_id", ondelete="CASCADE"), nullable=False),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("source_id", sa.String(128)),
            sa.Column("event_type", sa.String(16), nullable=False),
            sa.Column("event_fingerprint", sa.String(64), nullable=False),
            sa.Column("record_index", sa.Integer, nullable=False),
            sa.Column("event_date", TZ, nullable=False),
            sa.Column("payload_json", sa.JSON, nullable=False),
            sa.UniqueConstraint("batch_id", "event_type", "record_index", name="uq_history_batch_event"),
            sa.UniqueConstraint("well_id", "source_id", "event_type", "event_fingerprint",
                                name="uq_history_source_event"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_history_well_time ON history_events (well_id, event_date)")

    if not _has_table("analysis_runs"):
        op.create_table(
            "analysis_runs",
            sa.Column("analysis_id", sa.String(64), primary_key=True),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("request_id", sa.String(128)),
            sa.Column("created_at", TZ, nullable=False),
            sa.Column("status", sa.String(64), nullable=False),
            sa.Column("model_versions_json", sa.JSON, nullable=False),
            sa.Column("result_json", sa.JSON, nullable=False),
            sa.Column("agent_status", sa.String(64)),
            sa.Column("agent_updated_at", TZ),
            sa.Column("agent_response_json", sa.JSON),
            sa.Column("external_data_json", sa.JSON),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_analysis_runs_well_time ON analysis_runs (well_id, created_at)")

    if not _has_table("source_health"):
        op.create_table(
            "source_health",
            sa.Column("source_id", sa.String(128), primary_key=True),
            sa.Column("well_id", sa.String(128), primary_key=True),
            sa.Column("origin", sa.String(32), primary_key=True, nullable=False,
                      server_default="api_refresh"),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("message", sa.String(2000)),
            sa.Column("record_count", sa.Integer),
            sa.Column("retrieved_at", TZ, nullable=False),
        )
    else:
        # Databases from before `origin` existed: add it and rebuild the primary key once.
        op.execute("ALTER TABLE source_health ADD COLUMN IF NOT EXISTS origin VARCHAR(32) "
                   "NOT NULL DEFAULT 'api_refresh'")
        pk = sa.inspect(op.get_bind()).get_pk_constraint("source_health")
        if set(pk.get("constrained_columns") or []) != {"source_id", "well_id", "origin"}:
            if pk.get("name"):
                op.drop_constraint(pk["name"], "source_health", type_="primary")
            op.create_primary_key("source_health_pkey", "source_health",
                                  ["source_id", "well_id", "origin"])

    if not _has_table("source_documents"):
        op.create_table(
            "source_documents",
            sa.Column("source_id", sa.String(128), primary_key=True),
            sa.Column("well_id", sa.String(128), primary_key=True),
            sa.Column("payload_json", sa.JSON, nullable=False),
            sa.Column("updated_at", TZ, nullable=False),
        )


def downgrade() -> None:
    for table in ("source_documents", "source_health", "analysis_runs", "history_events",
                  "history_batches", "telemetry", "ingestion_batches"):
        op.execute(f"DROP TABLE IF EXISTS {table}")
