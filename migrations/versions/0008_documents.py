"""Documents (Excel, PDF, scans) with their extracted data, and the AI agent's well proposal.

Revision ID: 0008_documents
Revises: 0007_rating_operations
Create Date: 2026-10-19
"""
from alembic import op
import sqlalchemy as sa

revision = "0008_documents"
down_revision = "0007_rating_operations"
branch_labels = None
depends_on = None

RATING_COLUMNS = (
    ("ai_proposal", sa.JSON),
    ("ai_proposal_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if "documents" not in inspector.get_table_names():
        op.create_table(
            "documents",
            sa.Column("document_id", sa.String(36), primary_key=True),
            sa.Column("sha256", sa.String(64), nullable=False, unique=True),
            sa.Column("filename", sa.String(512), nullable=False),
            sa.Column("channel", sa.String(16), nullable=False),
            sa.Column("file_kind", sa.String(16), nullable=False),
            sa.Column("size_bytes", sa.BigInteger, nullable=False),
            sa.Column("storage_path", sa.String(1024), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("status_message", sa.String(2000)),
            sa.Column("pages", sa.Integer),
            sa.Column("doc_type", sa.String(64)),
            sa.Column("well_ids", sa.JSON),
            sa.Column("doc_date", sa.Date),
            sa.Column("text_method", sa.String(32)),
            sa.Column("text_chars", sa.Integer),
            sa.Column("text", sa.Text),
            sa.Column("extraction", sa.JSON),
            sa.Column("loaded", sa.JSON),
            sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True)),
            sa.Column("processed_at", sa.DateTime(timezone=True)),
        )
        op.create_index("idx_documents_status_created", "documents", ["status", "created_at"])
        op.create_index("idx_documents_created", "documents", ["created_at"])
    existing = {column["name"] for column in inspector.get_columns("well_rating")}
    for name, column_type in RATING_COLUMNS:
        if name not in existing:
            op.add_column("well_rating", sa.Column(name, column_type))


def downgrade() -> None:
    for name, _ in reversed(RATING_COLUMNS):
        op.drop_column("well_rating", name)
    op.drop_index("idx_documents_created", table_name="documents")
    op.drop_index("idx_documents_status_created", table_name="documents")
    op.drop_table("documents")
