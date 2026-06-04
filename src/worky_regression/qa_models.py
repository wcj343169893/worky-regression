"""QA 看板資料庫的 SQLAlchemy 模型（schema 的單一真實來源）。

schema 由這裡的模型定義，遷移由 Alembic 管理（autogenerate）。
`migrate()` 會先確保資料庫存在，再跑 `alembic upgrade head`，把 schema 帶到最新。
資料存取本身（QAStore）仍走顯式 SQL，沿用本專案 raw-SQL 風格；模型只負責「schema 形狀」。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import (
    BigInteger,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    func,
    text,
)
from sqlalchemy.dialects.mysql import JSON, LONGTEXT
from sqlalchemy.engine import URL, Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .config import Settings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = PROJECT_ROOT / "alembic.ini"
ALEMBIC_DIR = PROJECT_ROOT / "alembic"

_UNSET = object()


class Base(DeclarativeBase):
    pass


class QACase(Base):
    """用例註冊表：保證每筆用例都有穩定 id（YAML 仍是定義的單一真實來源）。"""
    __tablename__ = "qa_cases"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    file: Mapped[str] = mapped_column(String(255), nullable=False, server_default="")
    system: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="builtin")
    description: Mapped[str | None] = mapped_column(Text)
    step_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    yaml: Mapped[str | None] = mapped_column(LONGTEXT)
    created_at: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp())


class QARun(Base):
    """每次執行。"""
    __tablename__ = "qa_runs"

    run_id: Mapped[str] = mapped_column(String(160), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(128), nullable=False)
    system: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    description: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    passed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    total: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failed_at: Mapped[int | None] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(16), nullable=False, server_default="run")
    created_at: Mapped[datetime] = mapped_column(server_default=func.current_timestamp())

    __table_args__ = (Index("idx_case_started", "case_id", "started_at"),)


class QARunStep(Base):
    """每步結果。"""
    __tablename__ = "qa_run_steps"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(160), nullable=False)
    step_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="")
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    error: Mapped[str | None] = mapped_column(Text)
    observations: Mapped[dict | None] = mapped_column(JSON)

    __table_args__ = (Index("idx_run", "run_id", "step_index"),)


# ── 連線 / URL ───────────────────────────────────────────────────────────────
def db_url(settings: Settings, database=_UNSET) -> URL:
    """組 SQLAlchemy 連線 URL；database=None → 連到 server（不指定庫，供建庫用）。"""
    db = settings.qa_db_name if database is _UNSET else database
    return URL.create(
        "mysql+pymysql",
        username=settings.db_user,
        password=settings.db_pass,
        host=settings.db_host,
        port=settings.db_port,
        database=db,
        query={"charset": "utf8mb4"},
    )


_engines: dict[str, Engine] = {}


def get_engine(settings: Settings) -> Engine:
    key = f"{settings.db_host}:{settings.db_port}/{settings.qa_db_name}"
    if key not in _engines:
        _engines[key] = create_engine(db_url(settings), pool_pre_ping=True, future=True)
    return _engines[key]


def bootstrap_database(settings: Settings) -> None:
    """建庫（Alembic 只管表，不會建 database 本身）。"""
    eng = create_engine(db_url(settings, database=None), future=True)
    try:
        with eng.connect() as conn:
            conn.execute(text(
                f"CREATE DATABASE IF NOT EXISTS `{settings.qa_db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            conn.commit()
    finally:
        eng.dispose()


def migrate(settings: Settings | None = None) -> None:
    """確保資料庫存在並把 schema 帶到最新（alembic upgrade head）。"""
    from alembic import command
    from alembic.config import Config

    settings = settings or Settings.from_env()
    bootstrap_database(settings)
    cfg = Config(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    command.upgrade(cfg, "head")
