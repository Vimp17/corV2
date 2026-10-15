"""Rating fields for the simplified view: equipment run start, liquid rate, oil share, data gaps.

Revision ID: 0007_rating_operations
Revises: 0006_rating_protection
Create Date: 2026-10-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0007_rating_operations"
down_revision = "0006_rating_protection"
branch_labels = None
depends_on = None

NEW_COLUMNS = (
    ("installed_at", sa.DateTime(timezone=True)),
    ("liquid_rate_m3_day", sa.Float),
    ("oil_pct", sa.Float),
    ("data_gaps", sa.JSON),
)


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("well_rating")}
    for name, column_type in NEW_COLUMNS:
        if name not in existing:
            op.add_column("well_rating", sa.Column(name, column_type))


def downgrade() -> None:
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column("well_rating", name)
