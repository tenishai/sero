"""SQLAlchemy models for the backend datastore (D11).

The five baseline tables, defined with SQLAlchemy 2.0 typed declarative mapping.
These are pure schema: no engine, no connection, no live database is needed to
import or reason about them, which keeps them unit-testable and lets Alembic
autogenerate migrations from ``Base.metadata``.

Brand-neutral and secret-free by construction:

* ``tool_call_log`` - the audit trail. ``redacted_args`` holds arguments with
  secret material already removed by the caller; this table never stores a value.
* ``mcp_refresh_tokens`` - refresh-token *families* for the agent surface,
  recorded by ``jti`` and lifecycle timestamps. The token itself is never stored.
* ``mcp_allow_list`` - which identities may use the MCP surface.
* ``namespace_requests`` - the namespace-request workflow state.
* ``reveal_grants`` / ``reveal_flows`` - step-up "reveal" grants: a record that
  access was granted (by identity, time, and identity-provider assertion id),
  not the secret that was revealed.

Timestamps are timezone-aware and default to ``now()`` in the database. Identity
is an email string (the identity-provider subject), matching how auth is modeled
elsewhere; no provider-specific identifier is assumed.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Integer, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# A 64-bit autoincrement primary key. On PostgreSQL (the target) this is BIGINT;
# on SQLite (used in tests) it falls back to INTEGER, which is what SQLite
# autoincrements as a rowid. The same model then works on both dialects.
_BigIntPK = BigInteger().with_variant(Integer, "sqlite")


class Base(DeclarativeBase):
    """Declarative base for all datastore models."""


class ToolCallLog(Base):
    """Audit trail: one row per tool/API call. Never stores a secret value."""

    __tablename__ = "tool_call_log"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    user_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    tool_name: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)  # "ok" | "error"
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    response_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Arguments with secret material already removed by the caller. Never a value.
    redacted_args: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class McpRefreshToken(Base):
    """A refresh-token family for the agent (MCP) surface, by identifier only."""

    __tablename__ = "mcp_refresh_tokens"

    jti: Mapped[str] = mapped_column(Text, primary_key=True)
    family_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    user_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    client_id: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    scope: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class McpAllowListEntry(Base):
    """An identity permitted to use the MCP surface."""

    __tablename__ = "mcp_allow_list"

    email: Mapped[str] = mapped_column(Text, primary_key=True)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    added_by: Mapped[str] = mapped_column(Text, nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class NamespaceRequest(Base):
    """One namespace-request workflow row: who asked for what, and its status."""

    __tablename__ = "namespace_requests"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    requester_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    team_name: Mapped[str] = mapped_column(Text, nullable=False)
    environments: Mapped[list] = mapped_column(JSON, nullable=False)
    members: Mapped[list] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    ticket_ref: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class RevealFlow(Base):
    """An in-progress step-up reveal auth flow (short-lived PKCE state)."""

    __tablename__ = "reveal_flows"

    state: Mapped[str] = mapped_column(Text, primary_key=True)
    pkce_verifier: Mapped[str] = mapped_column(Text, nullable=False)
    requester_email: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RevealGrant(Base):
    """A step-up reveal grant: a record that access was granted, not the secret."""

    __tablename__ = "reveal_grants"

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    user_email: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    auth_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # The identity-provider assertion id that authorized the step-up, for audit
    # and replay protection. Not a secret.
    idp_assertion_id: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
