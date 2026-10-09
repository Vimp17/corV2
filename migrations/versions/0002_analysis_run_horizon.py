"""Record the horizon of each analysis run and index latest-run lookups.

Revision ID: 0002_analysis_run_horizon
Revises: 0001_baseline
Create Date: 2026-10-09
"""
from alembic import op

revision = "0002_analysis_run_horizon"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE analysis_runs ADD COLUMN IF NOT EXISTS horizon_days INTEGER")
    # Backfill from the stored result; skipped runs saved before this change have no horizon.
    op.execute("""
        UPDATE analysis_runs
        SET horizon_days = (result_json -> 'model_predictions' -> 0 ->> 'horizon_days')::int
        WHERE horizon_days IS NULL
          AND json_typeof(result_json -> 'model_predictions') = 'array'
          AND (result_json -> 'model_predictions' -> 0 ->> 'horizon_days') ~ '^[0-9]+$'
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_analysis_runs_well_horizon_time "
               "ON analysis_runs (well_id, horizon_days, created_at)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_analysis_runs_well_horizon_time")
    op.execute("ALTER TABLE analysis_runs DROP COLUMN IF EXISTS horizon_days")
