"""Alembic migration environment for the Ladon datastore (D11).

The database URL comes from the ``LADON_DATABASE_URL`` environment variable, not
from alembic.ini, so no connection string or credential is ever committed. The
target metadata is ``ladon.datastore.models.Base.metadata``, so migrations
autogenerate from the models.

Cloud-neutral: any PostgreSQL reachable by a SQLAlchemy URL works. Cloud-specific
auth (Cloud SQL IAM, RDS IAM) is a connection-layer concern handled later, not
here.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from ladon.datastore.models import Base

config = context.config

target_metadata = Base.metadata

DB_URL_ENV = "LADON_DATABASE_URL"


def _database_url() -> str:
    url = os.environ.get(DB_URL_ENV)
    if not url:
        raise RuntimeError(
            f"{DB_URL_ENV} is not set; point it at the target PostgreSQL, e.g. "
            "postgresql+psycopg://user@host:5432/ladon"
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to a script without a live connection (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    section = config.get_section(config.config_ini_section) or {}
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
