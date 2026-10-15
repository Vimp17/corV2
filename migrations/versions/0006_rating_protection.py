"""Protection status of a well and the НТЦ score before the protection adjustment.

Revision ID: 0006_rating_protection
Revises: 0005_rating_computed
Create Date: 2026-10-15
"""
from alembic import op
import sqlalchemy as sa

revision = "0006_rating_protection"
down_revision = "0005_rating_computed"
branch_labels = None
depends_on = None

NEW_COLUMNS = (("protection", sa.String(32)), ("model_ntc_raw", sa.Float))


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("well_rating")}
    for name, column_type in NEW_COLUMNS:
        if name not in existing:
            op.add_column("well_rating", sa.Column(name, column_type))


def downgrade() -> None:
    for name, _ in reversed(NEW_COLUMNS):
        op.drop_column("well_rating", name)
