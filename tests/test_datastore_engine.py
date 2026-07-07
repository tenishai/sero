"""Tests for the datastore connection/session layer (D11).

Exercised against SQLite via LADON_DATABASE_URL, so no PostgreSQL is needed. The
engine cache is reset around each test.
"""

import pytest
from sqlalchemy import select, text

from ladon.datastore.engine import (
    build_engine,
    database_url,
    engine,
    is_configured,
    reset,
    session_factory,
)
from ladon.datastore.models import Base, McpAllowListEntry


@pytest.fixture(autouse=True)
def _reset_engine(monkeypatch):
    monkeypatch.delenv("LADON_DATABASE_URL", raising=False)
    reset()
    yield
    reset()


def test_unconfigured_returns_none(monkeypatch):
    assert database_url() is None
    assert is_configured() is False
    assert engine() is None
    assert session_factory() is None


def test_configured_builds_engine(monkeypatch):
    monkeypatch.setenv("LADON_DATABASE_URL", "sqlite:///:memory:")
    assert is_configured() is True
    eng = engine()
    assert eng is not None
    # Cached: same object on the second call.
    assert engine() is eng


def test_session_factory_round_trips(monkeypatch):
    monkeypatch.setenv("LADON_DATABASE_URL", "sqlite:///:memory:")
    eng = engine()
    Base.metadata.create_all(eng)
    factory = session_factory()
    assert factory is not None
    with factory() as s:
        s.add(McpAllowListEntry(email="op@example.com", added_by="admin@example.com"))
        s.commit()
    with factory() as s:
        got = s.scalars(select(McpAllowListEntry)).one()
        assert got.email == "op@example.com"


def test_reset_rebuilds_from_current_env(monkeypatch):
    monkeypatch.setenv("LADON_DATABASE_URL", "sqlite:///:memory:")
    first = engine()
    reset()
    second = engine()
    assert second is not None
    assert second is not first  # a new engine after reset


def test_build_engine_accepts_creator(monkeypatch):
    # A cloud adapter would pass a creator; here a trivial one proves it is wired
    # through to a working engine.
    import sqlite3

    def creator():
        return sqlite3.connect(":memory:")

    eng = build_engine("sqlite://", creator=creator)
    with eng.connect() as conn:
        assert conn.execute(text("select 1")).scalar() == 1
