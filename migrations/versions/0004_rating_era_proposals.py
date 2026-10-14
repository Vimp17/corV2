"""Well rating inputs (approved template) and ERA repair proposals sent through Промайн.

Revision ID: 0004_rating_era_proposals
Revises: 0003_monitor_jobs
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0004_rating_era_proposals"
down_revision = "0003_monitor_jobs"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if not inspector.has_table("well_rating"):
        op.create_table(
            "well_rating",
            sa.Column("well_id", sa.String(128), primary_key=True),
            sa.Column("field", sa.String(256)),
            sa.Column("equipment", sa.String(256)),
            sa.Column("in_operation", sa.Boolean),
            sa.Column("sampling", sa.Boolean),
            sa.Column("corrosive_environment", sa.Boolean),
            sa.Column("tonnage_t", sa.Float),
            sa.Column("noo_days", sa.Float),
            sa.Column("demount_act", sa.Boolean),
            sa.Column("demount_corrosion", sa.Boolean),
            sa.Column("base_act", sa.Boolean),
            sa.Column("base_corrosion", sa.Boolean),
            sa.Column("category", sa.String(32)),
            sa.Column("model_ntc", sa.Float),
            sa.Column("model_math", sa.Float),
            sa.Column("plan_date", sa.Date),
            sa.Column("promine_status", sa.String(32)),
            sa.Column("comment", sa.String(2000)),
            sa.Column("assessed_at", TZ),
            sa.Column("updated_at", TZ, nullable=False),
            sa.Column("source", sa.String(32), nullable=False),
        )
    if not inspector.has_table("era_proposals"):
        op.create_table(
            "era_proposals",
            sa.Column("proposal_id", sa.String(64), primary_key=True),
            sa.Column("well_id", sa.String(128), nullable=False),
            sa.Column("change_type", sa.String(32), nullable=False),
            sa.Column("reason", sa.String(4000), nullable=False),
            sa.Column("proposed_date", sa.Date),
            sa.Column("author", sa.String(200)),
            sa.Column("created_at", TZ, nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("transport", sa.String(16), nullable=False),
            sa.Column("attempts", sa.Integer, nullable=False),
            sa.Column("sent_at", TZ),
            sa.Column("external_id", sa.String(256)),
            sa.Column("error", sa.String(2000)),
            sa.Column("subject", sa.String(500), nullable=False),
            sa.Column("body", sa.String(20000), nullable=False),
            sa.Column("rating_json", sa.JSON),
        )
    op.execute("CREATE INDEX IF NOT EXISTS idx_era_proposals_well_time "
               "ON era_proposals (well_id, created_at)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS era_proposals")
    op.execute("DROP TABLE IF EXISTS well_rating")
