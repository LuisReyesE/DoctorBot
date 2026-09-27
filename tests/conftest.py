from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

# Config de test antes de importar la app
os.environ["APP_ENV"] = "development"
os.environ["MESSAGING_PROVIDER"] = "mock"
os.environ["WHATSAPP_PROVIDER"] = "mock"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SEED_DOCTOR_ID"] = "51999900001"
os.environ["SEED_DOCTOR_PHONE"] = "51999900001"
os.environ["SEED_DOCTOR_NAME"] = "Dr. Demo"
os.environ["REQUIRE_SUBSCRIPTION"] = "false"
os.environ["TRIAL_DAYS"] = "3"
os.environ["PANEL_TOKEN"] = "admin"
os.environ["PANEL_ENABLED"] = "true"
os.environ["REMINDER_AUTO"] = "false"


from app.config import get_settings  # noqa: E402
from app.db import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402
from app.providers import get_mock_client  # noqa: E402
from app.services.seed import seed_demo_doctor  # noqa: E402

get_settings.cache_clear()


@pytest.fixture()
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    settings = get_settings()
    seed_demo_doctor(session, settings)
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client(db_session):
    def _override_get_db():
        try:
            yield db_session
        finally:
            pass

    app.dependency_overrides[get_db] = _override_get_db
    get_mock_client().clear()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()
    get_mock_client().clear()
