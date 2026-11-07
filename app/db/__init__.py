"""Database of the service: Microsoft SQL Server 2016+ (compatibility level 130+ for the JSON functions).

MAI_DATABASE_URL connects through pyodbc and an installed "ODBC Driver 17/18 for SQL Server"
(mssql+pyodbc://…?driver=…) or through pymssql, which needs no ODBC driver (mssql+pymssql://…);
MAI_DATABASE_SCHEMA names the schema of the tables (empty: the default schema of the login, usually
dbo). At start (init_db) the tables are checked against their definitions (schema.py): missing tables are
created, columns a later version defines are added, tables and columns of earlier versions are
dropped, and the check is repeated. With everything in place nothing is changed, so a login with
read and write rights only is enough.

Text is NVARCHAR with a binary collation, so keys compare exactly whatever the collation of the
database (DEMO-1 and demo-1 stay two wells); moments are DATETIME2 in UTC, read back with tzinfo=UTC;
JSON is NVARCHAR(MAX).
"""

# The modules: schema (tables), base (connection, start-up check, locks, deadlocks, MERGE), sources (source
# data), rating (daily cycle jobs, the stored rating), documents. Callers use them as app.db.<name>
# (app.db.documents is the module; the table is app.db.schema.documents).
from app.db.schema import (
    metadata, history_batches, history_events, ingestion_batches, monitor_jobs, source_documents,
    source_health, telemetry, well_rating)
from app.db.base import (
    advisory_lock, check_database, connect_args, describe_changes, engine, init_db, MONITOR_LOCK_KEY,
    qualified, schema_changes)
from app.db.sources import (
    effective_source_statuses, forget_well, known_well_ids, list_source_statuses, list_wells,
    load_extra_measurements, load_history, load_history_events, load_source_payloads, load_telemetry,
    record_source_status, save_batch, save_history, save_source_payload, well_has_data)
from app.db.rating import (
    create_monitor_job, get_monitor_job, get_rating_row, list_rating_rows, rating_scores, save_ai_proposal,
    save_computed_rating, update_monitor_job, update_rating_row)
from app.db.documents import (
    claim_next_document, create_document, delete_document_data, delete_document_row, document_status_counts,
    get_document, list_documents, requeue_stale_documents, update_document)

__all__ = [
    "metadata", "history_batches", "history_events", "ingestion_batches", "monitor_jobs", "source_documents",
    "source_health", "telemetry", "well_rating", "advisory_lock", "check_database", "connect_args",
    "describe_changes", "engine", "init_db", "MONITOR_LOCK_KEY", "qualified", "schema_changes",
    "effective_source_statuses", "forget_well", "known_well_ids", "list_source_statuses", "list_wells",
    "load_extra_measurements", "load_history", "load_history_events", "load_source_payloads", "load_telemetry",
    "record_source_status", "save_batch", "save_history", "save_source_payload", "well_has_data",
    "create_monitor_job", "get_monitor_job", "get_rating_row", "list_rating_rows", "rating_scores",
    "save_ai_proposal", "save_computed_rating", "update_monitor_job", "update_rating_row",
    "claim_next_document", "create_document", "delete_document_data", "delete_document_row",
    "document_status_counts", "get_document", "list_documents", "requeue_stale_documents", "update_document",
]
