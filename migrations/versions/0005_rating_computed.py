"""Well rating computed from source data: provenance, measure tracking, template data removed.

Rows loaded from the retired rating template are dropped for wells that have no source data;
for the other wells their template values are cleared (operator fields are kept) so the next
computation fills them from DARS, ХИМИК and ERA.

Revision ID: 0005_rating_computed
Revises: 0004_rating_era_proposals
Create Date: 2026-10-14
"""
from alembic import op
import sqlalchemy as sa

revision = "0005_rating_computed"
down_revision = "0004_rating_era_proposals"
branch_labels = None
depends_on = None

TZ = sa.DateTime(timezone=True)
NEW_COLUMNS = (
    ("measure", sa.String(32)),
    ("measure_since", TZ),
    ("provenance", sa.JSON),
    ("computed_at", TZ),
    ("skip_reason", sa.String(2000)),
    ("analysis_id", sa.String(64)),
)
COMPUTED = ("field", "equipment", "in_operation", "sampling", "corrosive_environment", "tonnage_t",
            "noo_days", "demount_act", "demount_corrosion", "base_act", "base_corrosion", "category",
            "model_ntc", "model_math")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    existing = {column["name"] for column in inspector.get_columns("well_rating")}
    for name, column_type in NEW_COLUMNS:
        if name not in existing:
            op.add_column("well_rating", sa.Column(name, column_type))
    op.execute("""
        DELETE FROM well_rating r
        WHERE NOT EXISTS (SELECT 1 FROM telemetry t WHERE t.well_id = r.well_id)
          AND NOT EXISTS (SELECT 1 FROM history_events h WHERE h.well_id = r.well_id)
    """)
    op.execute("UPDATE well_rating SET " + ", ".join(f"{name} = NULL" for name in COMPUTED)
               + ", source = 'legacy_template' WHERE computed_at IS NULL")
    op.execute("DELETE FROM source_health WHERE source_id = 'RATING'")


def downgrade() -> None:
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column("well_rating", name)
