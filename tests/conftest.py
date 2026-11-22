from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db, make_engine
from app.main import app
from app.models import Base, Source

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings():
    return Settings(_env_file=None, api_key="test-key", request_interval=0.1)


@pytest.fixture
def db(tmp_path):
    engine = make_engine("sqlite:///" + str(tmp_path / "test.db"))
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session
    engine.dispose()


@pytest.fixture
def source(db):
    source = Source(name="ФСП", adapter="fsp", url="https://fsp-russia.ru/press/news/")
    db.add(source)
    db.commit()
    return source


@pytest.fixture
def client(db, settings, monkeypatch):
    monkeypatch.setattr("app.main.get_settings", lambda: settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app, headers={"X-API-Key": "test-key"}) as client:
        yield client
    app.dependency_overrides.clear()
