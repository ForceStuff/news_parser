import os
import subprocess
import sys

from sqlalchemy import create_engine, inspect, text


def test_migrations_upgrade_downgrade_roundtrip(tmp_path):
    url = "sqlite:///" + str(tmp_path / "migrations.db")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )

    alembic("upgrade", "head")
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM sources")) == 1
    assert {"articles", "article_images", "jobs", "exports"} <= set(
        inspect(engine).get_table_names()
    )
    engine.dispose()
    alembic("check")
    alembic("downgrade", "base")
    alembic("upgrade", "head")
