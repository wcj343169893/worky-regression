"""Alembic 環境：target_metadata 取自 SQLAlchemy 模型，連線 URL 從 .env（Settings）動態組出。

支援 CLI（`alembic upgrade head` / `alembic revision --autogenerate`）與程式化呼叫
（qa_models.migrate()）。線上模式會先確保 database 存在再連線。
"""
from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool

# 確保 src/ 在 path 上（CLI 直接跑 alembic 時）
SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from worky_regression import qa_models  # noqa: E402
from worky_regression.config import Settings  # noqa: E402

config = context.config
if config.config_file_name is not None:
    try:
        fileConfig(config.config_file_name)
    except Exception:  # noqa: BLE001 — logging 設定缺失不致命
        pass

target_metadata = qa_models.Base.metadata
_settings = Settings.from_env()


def run_migrations_offline() -> None:
    url = qa_models.db_url(_settings).render_as_string(hide_password=False)
    context.configure(
        url=url, target_metadata=target_metadata,
        literal_binds=True, dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    qa_models.bootstrap_database(_settings)  # Alembic 不建 database 本身
    connectable = create_engine(qa_models.db_url(_settings), poolclass=pool.NullPool, future=True)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
