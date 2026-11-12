"""Tables of the service and the column types they use (SQL Server)."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (BigInteger, Boolean, Column, Date, Float, ForeignKey, Index, Integer, JSON, MetaData,
                        Table, Unicode, UnicodeText, UniqueConstraint)
from sqlalchemy.dialects.mssql import DATETIME2
from sqlalchemy.types import TypeDecorator


# Exact comparison and ordering of text, independent of the collation of the database.
BINARY_COLLATION = "Latin1_General_100_BIN2"
# Tables and columns of earlier versions, dropped at start: a NOT NULL column the application no
# longer writes would make its inserts fail.
OBSOLETE_TABLES = ("analysis_runs", "era_proposals")
OBSOLETE_COLUMNS = {"monitor_jobs": ("horizon_days",),
                    "well_rating": ("skip_reason", "analysis_id")}
# Unique constraints of SQL Server let one NULL in; rows without a valid source are keyed under this
# sentinel, so identical rows are never stored twice.
_UNSPECIFIED_SOURCE = "__unspecified__"


class UTCDateTime(TypeDecorator):
    """A moment stored as UTC in DATETIME2 and returned with tzinfo=UTC (naive values are UTC)."""

    impl = DATETIME2
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        return value if value.tzinfo is None else value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def NVarchar(length: int | None = None):  # noqa: N802  (a column type, named like the SQLAlchemy ones)
    """NVARCHAR(length); NVARCHAR(MAX) without a length or beyond 4000; binary collation."""
    if length and length <= 4000:
        return Unicode(length, collation=BINARY_COLLATION)
    return UnicodeText(collation=BINARY_COLLATION)

metadata = MetaData()
ingestion_batches = Table(
    "ingestion_batches", metadata,
    Column("batch_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), nullable=False),
    Column("record_count", Integer, nullable=False), Column("created_at", UTCDateTime, nullable=False),
    Column("payload_sha256", NVarchar(64), nullable=False),
)
telemetry = Table(
    "telemetry", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", NVarchar(128), ForeignKey("ingestion_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", NVarchar(128), nullable=False), Column("source_id", NVarchar(128)),
    Column("source_occurrence", Integer), Column("record_index", Integer, nullable=False),
    Column("timestamp", UTCDateTime, nullable=False), Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "record_index", name="uq_telemetry_batch_record"),
    UniqueConstraint("well_id", "source_id", "timestamp", "source_occurrence",
                     name="uq_telemetry_source_observation"),
)
Index("idx_telemetry_well_time", telemetry.c.well_id, telemetry.c.timestamp)
history_batches = Table(
    "history_batches", metadata,
    Column("batch_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), nullable=False),
    Column("work_count", Integer, nullable=False), Column("failure_count", Integer, nullable=False),
    Column("payload_sha256", NVarchar(64), nullable=False), Column("created_at", UTCDateTime, nullable=False),
)
history_events = Table(
    "history_events", metadata,
    Column("id", BigInteger, primary_key=True, autoincrement=True),
    Column("batch_id", NVarchar(128), ForeignKey("history_batches.batch_id", ondelete="CASCADE"), nullable=False),
    Column("well_id", NVarchar(128), nullable=False), Column("source_id", NVarchar(128)),
    Column("event_type", NVarchar(16), nullable=False), Column("event_fingerprint", NVarchar(64), nullable=False),
    Column("record_index", Integer, nullable=False), Column("event_date", UTCDateTime, nullable=False),
    Column("payload_json", JSON, nullable=False),
    UniqueConstraint("batch_id", "event_type", "record_index", name="uq_history_batch_event"),
    UniqueConstraint("well_id", "source_id", "event_type", "event_fingerprint",
                     name="uq_history_source_event"),
)
Index("idx_history_well_time", history_events.c.well_id, history_events.c.event_date)
source_health = Table(
    "source_health", metadata,
    Column("source_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), primary_key=True),
    Column("origin", NVarchar(32), primary_key=True, nullable=False, server_default="api_refresh"),
    Column("status", NVarchar(32), nullable=False), Column("message", NVarchar(2000)),
    Column("record_count", Integer), Column("retrieved_at", UTCDateTime, nullable=False),
)
monitor_jobs = Table(
    "monitor_jobs", metadata,
    Column("job_id", NVarchar(64), primary_key=True), Column("status", NVarchar(32), nullable=False),
    Column("created_at", UTCDateTime, nullable=False), Column("started_at", UTCDateTime), Column("completed_at", UTCDateTime),
    Column("result_json", JSON), Column("error", NVarchar(2000)),
)
# Current rating per well. Evidence and the НТЦ score are computed from source data by
# app.core.rating_builder (provenance records the source and date of each value); plan_date,
# promine_status and comment are operator fields (PATCH /rating/wells/{id}) never overwritten by a
# recomputation. The index, measure and deadline are derived in app.core.rating.
well_rating = Table(
    "well_rating", metadata,
    Column("well_id", NVarchar(128), primary_key=True),
    Column("field", NVarchar(256)), Column("equipment", NVarchar(256)),
    Column("in_operation", Boolean), Column("sampling", Boolean),
    Column("corrosive_environment", Boolean),
    Column("tonnage_t", Float), Column("noo_days", Float),
    Column("demount_act", Boolean), Column("demount_corrosion", Boolean),
    Column("base_act", Boolean), Column("base_corrosion", Boolean),
    Column("category", NVarchar(32)),
    Column("model_ntc", Float), Column("model_math", Float),
    Column("plan_date", Date), Column("promine_status", NVarchar(32)),
    Column("comment", NVarchar(2000)),
    Column("assessed_at", UTCDateTime),
    Column("updated_at", UTCDateTime, nullable=False),
    Column("source", NVarchar(32), nullable=False),
    Column("measure", NVarchar(32)), Column("measure_since", UTCDateTime),
    Column("provenance", JSON), Column("computed_at", UTCDateTime),
    Column("protection", NVarchar(32)), Column("model_ntc_raw", Float),
    Column("installed_at", UTCDateTime), Column("liquid_rate_m3_day", Float),
    Column("oil_pct", Float), Column("data_gaps", JSON),
    # Latest proposal drafted by the AI agent from all well data (app.core.proposal_agent).
    Column("ai_proposal", JSON), Column("ai_proposal_at", UTCDateTime),
)
source_documents = Table(
    "source_documents", metadata,
    Column("source_id", NVarchar(128), primary_key=True), Column("well_id", NVarchar(128), primary_key=True),
    Column("payload_json", JSON, nullable=False), Column("updated_at", UTCDateTime, nullable=False),
)
# Documents received as files (manual upload or the inbox folder): the original is stored on
# disk, the extracted text and fields here; accepted data belongs to document_contributions.
documents = Table(
    "documents", metadata,
    Column("document_id", NVarchar(36), primary_key=True),
    Column("sha256", NVarchar(64), nullable=False, unique=True),
    Column("filename", NVarchar(512), nullable=False),
    Column("channel", NVarchar(16), nullable=False),
    Column("file_kind", NVarchar(16), nullable=False),
    Column("size_bytes", BigInteger, nullable=False),
    Column("storage_path", NVarchar(1024), nullable=False),
    Column("status", NVarchar(32), nullable=False),
    Column("status_message", NVarchar(2000)),
    Column("pages", Integer),
    Column("doc_type", NVarchar(64)),
    Column("well_ids", JSON),
    Column("doc_date", Date),
    Column("text_method", NVarchar(32)),
    Column("text_chars", Integer),
    Column("text", NVarchar()),
    Column("extraction", JSON),
    Column("loaded", JSON),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("claim_id", NVarchar(36)),
    Column("created_at", UTCDateTime, nullable=False),
    Column("started_at", UTCDateTime),
    Column("processed_at", UTCDateTime),
)
Index("idx_documents_status_created", documents.c.status, documents.c.created_at)
Index("idx_documents_created", documents.c.created_at)

# Scenario rates kept separate from probabilities and measured telemetry.
co2_assessments = Table(
    "co2_assessments", metadata,
    Column("assessment_id", NVarchar(64), primary_key=True),
    Column("well_id", NVarchar(128), nullable=False),
    Column("state_at", UTCDateTime), Column("created_at", UTCDateTime, nullable=False),
    Column("model_version", NVarchar(64), nullable=False), Column("result_json", JSON, nullable=False),
)
Index("idx_co2_well_created", co2_assessments.c.well_id, co2_assessments.c.created_at)

# A document owns its contribution; independent imports keep their existing tables.
document_contributions = Table(
    "document_contributions", metadata,
    Column("document_id", NVarchar(36), ForeignKey("documents.document_id", ondelete="CASCADE"), primary_key=True),
    Column("well_id", NVarchar(128), primary_key=True),
    Column("source_id", NVarchar(128), primary_key=True),
    Column("data", JSON, nullable=False), Column("updated_at", UTCDateTime, nullable=False),
)
Index("idx_document_contributions_well", document_contributions.c.well_id)

rating_recompute_queue = Table(
    "rating_recompute_queue", metadata,
    Column("well_id", NVarchar(128), primary_key=True),
    Column("revision", NVarchar(36), nullable=False),
    Column("queued_at", UTCDateTime, nullable=False), Column("last_error", NVarchar(2000)),
)
