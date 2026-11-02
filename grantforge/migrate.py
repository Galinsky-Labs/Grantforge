"""Database schema migrations (Alembic), applied automatically at startup.

* A new database is created by running every migration from the start.
* A database created by GrantForge v0.1 (which used create_all and has no
  alembic_version table) is stamped as revision 0001 (the v0.1 schema) and
  then upgraded, so existing data is kept.
"""
from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect
from sqlalchemy.engine import Engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
V01_REVISION = "0001"


def alembic_config(engine: Engine, connection=None, target_metadata=None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", str(engine.url))
    if connection is not None:
        cfg.attributes["connection"] = connection
    if target_metadata is not None:
        cfg.attributes["target_metadata"] = target_metadata
    return cfg


def upgrade_to_head(engine: Engine) -> None:
    with engine.begin() as conn:
        tables = set(inspect(conn).get_table_names())
        cfg = alembic_config(engine, conn)
        if "projects" in tables and "alembic_version" not in tables:
            command.stamp(cfg, V01_REVISION)
        command.upgrade(cfg, "head")
