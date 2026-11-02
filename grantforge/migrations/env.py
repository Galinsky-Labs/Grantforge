"""Alembic environment. Run programmatically by grantforge.migrate; no alembic.ini needed."""
from alembic import context

config = context.config
target_metadata = config.attributes.get("target_metadata")
if target_metadata is None:
    from grantforge.db import Base

    target_metadata = Base.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        from sqlalchemy import create_engine

        connection = create_engine(config.get_main_option("sqlalchemy.url")).connect()
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,  # SQLite cannot ALTER most things; batch mode rebuilds tables safely
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


run_migrations_online()
