"""Database engine and session management.

Uses SQLite with WAL journal mode for concurrent read access.
Default path: ~/.local/state/mediasorter/state.db
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

DEFAULT_DB_PATH = Path.home() / ".local" / "state" / "mediasorter" / "state.db"


def get_engine(db_path: Path | None = None, echo: bool = False):
    """Create a SQLite engine with WAL mode enabled."""
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(f"sqlite:///{path}", echo=echo)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn, _connection_record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def create_tables(engine) -> None:
    """Create all tables (idempotent)."""
    SQLModel.metadata.create_all(engine)


@contextmanager
def get_session(engine) -> Generator[Session, None, None]:
    """Yield a session, auto-commit on success, rollback on error."""
    with Session(engine) as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
