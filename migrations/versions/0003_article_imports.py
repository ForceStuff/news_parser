"""Track successful article imports and pending covers."""

import sqlalchemy as sa
from alembic import op

revision = "0003_article_imports"
down_revision = "0002_fsp_source"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_jobs_kind_source_created", "jobs", ["kind", "source_id", "created_at"])
    op.create_table(
        "article_imports",
        sa.Column("article_id", sa.String(36), sa.ForeignKey("articles.id"), primary_key=True),
        sa.Column("news_id", sa.String(36), nullable=True),
        sa.Column("cover_attached", sa.Boolean(), nullable=False),
    )


def downgrade():
    op.drop_table("article_imports")
    op.drop_index("ix_jobs_kind_source_created", table_name="jobs")
