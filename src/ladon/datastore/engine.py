"""Connection and session management for the backend datastore (D11).

This turns the ``LADON_DATABASE_URL`` connection string into a pooled SQLAlchemy
engine and a session factory. It is deliberately cloud-neutral: any PostgreSQL
reachable by a SQLAlchemy URL works, and the URL is the only configuration. The
source platform bound this to a specific cloud's managed database and IAM auth;
here that is left to an optional ``creator`` hook so a cloud adapter (Cloud SQL
IAM, RDS IAM) can be added later without changing callers.

The datastore is gracefully optional. When ``LADON_DATABASE_URL`` is unset (local
development, tests that do not need it), ``engine()`` and ``session_factory()``
return ``None``; callers guard on that rather than crashing. The same env var
drives Alembic (see migrations/env.py), so migrations and the app agree on the
target with no second source of truth.

No secret is logged. A connection URL may embed a password; this module never
prints the URL and never logs it on error.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

DB_URL_ENV = "LADON_DATABASE_URL"

# Sound generic pool defaults: verify a connection before use (survives a DB
# restart or dropped idle connection) and recycle connections well under common
# server-side idle timeouts.
DEFAULT_POOL_PRE_PING = True
DEFAULT_POOL_RECYCLE = 1800  # seconds

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def database_url() -> str | None:
    """The configured database URL, or None when the datastore is not configured."""
    return os.environ.get(DB_URL_ENV) or None


def is_configured() -> bool:
    """True when a database URL is set. Callers guard on this before using the DB."""
    return database_url() is not None


def build_engine(url: str, *, creator: Callable[[], object] | None = None) -> Engine:
    """Build a pooled engine for ``url``.

    ``creator``, when given, is a zero-arg callable returning a raw DBAPI
    connection; a cloud adapter uses it to inject IAM-authenticated connections
    while keeping the rest of this layer unchanged. The URL still selects the
    dialect/driver.
    """
    kwargs: dict = {
        "pool_pre_ping": DEFAULT_POOL_PRE_PING,
        "pool_recycle": DEFAULT_POOL_RECYCLE,
    }
    if creator is not None:
        kwargs["creator"] = creator
    return create_engine(url, **kwargs)


def engine() -> Engine | None:
    """The process-wide engine, built lazily, or None if the datastore is unconfigured."""
    global _engine
    if _engine is not None:
        return _engine
    url = database_url()
    if url is None:
        return None
    _engine = build_engine(url)
    return _engine


def session_factory() -> sessionmaker[Session] | None:
    """The process-wide session factory, or None if the datastore is unconfigured."""
    global _session_factory
    if _session_factory is not None:
        return _session_factory
    eng = engine()
    if eng is None:
        return None
    _session_factory = sessionmaker(eng, expire_on_commit=False)
    return _session_factory


def reset() -> None:
    """Dispose the engine and clear the cached engine/session factory.

    For tests and for reconfiguration: the next ``engine()`` rebuilds from the
    current environment. Safe to call when nothing was built.
    """
    global _engine, _session_factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
