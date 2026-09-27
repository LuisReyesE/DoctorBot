from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings

settings = get_settings()

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)


@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:  # type: ignore[no-untyped-def]
    if settings.database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _sqlite_add_missing_columns()


def _sqlite_add_missing_columns() -> None:
    """Añade columnas nuevas en SQLite sin migraciones formales (dev/MVP)."""
    if not settings.database_url.startswith("sqlite"):
        return
    alters = [
        ("users", "subscription_status", "VARCHAR(20) DEFAULT 'none'"),
        ("users", "subscription_plan", "VARCHAR(20)"),
        ("users", "subscription_expires_at", "DATETIME"),
        ("consultations", "orientation_text", "TEXT"),
        ("consultations", "pdf_path", "VARCHAR(500)"),
        ("consultations", "reminder_sent_at", "DATETIME"),
        ("doctors", "specialty", "VARCHAR(40) DEFAULT 'general'"),
        ("doctors", "last_assigned_at", "DATETIME"),
        ("users", "yape_name", "VARCHAR(120)"),
        ("users", "yape_phone", "VARCHAR(32)"),
        ("payments", "voucher_path", "VARCHAR(500)"),
        ("payments", "notes", "VARCHAR(500)"),
        ("payments", "yape_name", "VARCHAR(120)"),
        ("payments", "yape_phone", "VARCHAR(32)"),
        ("payments", "verifier_id", "INTEGER"),
        ("users", "document_type", "VARCHAR(8)"),
        ("users", "document_number", "VARCHAR(20)"),
        ("users", "consent_version", "VARCHAR(16)"),
        ("users", "consent_accepted_at", "DATETIME"),
        ("payments", "consent_version", "VARCHAR(16)"),
    ]
    with engine.begin() as conn:
        for table, column, coltype in alters:
            try:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
            except Exception:
                # columna ya existe u otro error benigno en create_all fresco
                pass
