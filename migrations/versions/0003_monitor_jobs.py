"""Background monitor jobs.

Revision ID: 0003_monitor_jobs
Revises: 0002_analysis_run_horizon
Create Date: 2026-10-09
"""
from alembic import op
import sqlalchemy as sa

revision = "0003_monitor_jobs"
down_revision = "0002_analysis_run_horizon"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("monitor_jobs"):
        return
    op.create_table(
        "monitor_jobs",
        sa.Column("job_id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("result_json", sa.JSON),
        sa.Column("error", sa.String(2000)),
    )


def downgrade() -> None:
    op.drop_table("monitor_jobs")
