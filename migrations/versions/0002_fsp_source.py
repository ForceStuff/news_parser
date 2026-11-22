"""Register the first source without making an external HTTP request."""

from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import op

revision = "0002_fsp_source"
down_revision = "0a633d9bc115"
branch_labels = None
depends_on = None


def upgrade():
    sources = sa.table(
        "sources",
        sa.column("id", sa.String),
        sa.column("name", sa.String),
        sa.column("adapter", sa.String),
        sa.column("url", sa.String),
        sa.column("enabled", sa.Boolean),
        sa.column("created_at", sa.DateTime),
    )
    op.bulk_insert(
        sources,
        [
            {
                "id": "c9d743f6-c603-4d37-a911-51f98e718c60",
                "name": "ФСП России — новости",
                "adapter": "fsp",
                "url": "https://fsp-russia.ru/press/news/",
                "enabled": True,
                "created_at": datetime.now(timezone.utc),
            }
        ],
    )


def downgrade():
    op.execute("DELETE FROM sources WHERE id = 'c9d743f6-c603-4d37-a911-51f98e718c60'")
