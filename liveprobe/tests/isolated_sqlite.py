"""정돈-02~08 시험 전용: 새로 만든 임시 SQLite 파일에만 LiveProbe K8s 스키마를 만든다.

기존 conftest의 PostgreSQL schema 삭제 fixture와 운영/시험 DB를 쓰지 않는다. PostgreSQL 전용 열 형식(JSONB·TSVECTOR·
pgvector)은 SQLite DDL에서만 일반 형식으로 대체한다. 계산 열(tsv)은 SQLite에서 만들지 않는다.
"""
from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import BigInteger, MetaData, create_engine, event
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker


@compiles(JSONB, "sqlite")
def _jsonb_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "JSON"


@compiles(TSVECTOR, "sqlite")
def _tsv_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    return "TEXT"


@compiles(BigInteger, "sqlite")
def _bigint_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
    # SQLite only auto-assigns ids for "INTEGER PRIMARY KEY"; BIGINT ids would stay NULL.
    return "INTEGER"


try:
    from pgvector.sqlalchemy import Vector

    @compiles(Vector, "sqlite")
    def _vector_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL hook
        return "BLOB"
except Exception:  # pragma: no cover
    pass


def _sqlite_compat(metadata):
    """SQLite cannot evaluate to_tsvector(); the generated column is display-irrelevant for these tests.

    PostgreSQL partial unique indexes keep their WHERE clause on SQLite (sqlite_where) instead of silently becoming
    full unique indexes. Only the disposable SQLite metadata copy is modified."""
    for table in metadata.tables.values():
        for column in table.columns:
            if column.computed is not None:
                column.computed = None
                column.server_default = None
        for index in table.indexes:
            where = index.dialect_options["postgresql"]._non_defaults.get("where")
            if where is not None:
                index.dialect_options["sqlite"]["where"] = where


def _guard(path: Path) -> Path:
    path = path.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if tmp not in path.parents:
        raise RuntimeError("isolated SQLite must live under the test temp directory")
    return path


@contextmanager
def isolated_k8s_db(tmp_path: Path):
    """Bind app.db to a brand-new SQLite file and yield its URL. Never accepts an existing database."""
    from app import db
    from app import models  # noqa: F401 - registers tables
    from app import change_models  # noqa: F401 - registers change tables
    from app.models import Base

    path = _guard(Path(tmp_path) / "liveprobe-isolated.sqlite")
    if path.exists():
        raise RuntimeError("isolated SQLite file already exists")
    url = f"sqlite:///{path}"
    engine = create_engine(url, connect_args={"check_same_thread": False})

    @event.listens_for(engine, "connect")
    def _fk(dbapi_conn, _record):  # pragma: no cover - connection hook
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    sqlite_schema = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(sqlite_schema)
    _sqlite_compat(sqlite_schema)
    sqlite_schema.create_all(engine)
    old_engine, old_session = db._engine, db._Session
    db._engine, db._Session = engine, sessionmaker(engine, expire_on_commit=False)
    try:
        yield url
    finally:
        db._engine, db._Session = old_engine, old_session
        engine.dispose()


def table_counts(tables=None) -> dict[str, int]:
    """Row count of every table: used to prove a read path wrote nothing."""
    from sqlalchemy import func, select
    from app import db
    from app.models import Base
    out = {}
    with db.engine().connect() as c:
        for name, table in sorted(Base.metadata.tables.items()):
            if tables and name not in tables:
                continue
            out[name] = c.execute(select(func.count()).select_from(table)).scalar_one()
    return out


def env_without_live_targets() -> dict:
    """LIVEPROBE_* values could point at real services; tests start from a clean slate."""
    return {k: v for k, v in os.environ.items() if not k.startswith(("LIVEPROBE_", "APPLICATION_"))}
