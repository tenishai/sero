"""The backend datastore (D11): durable, non-secret operational state.

Sero's backend keeps state that is operational metadata, never secret values:
the audit log, MCP refresh-token families and allow list, the namespace-request
workflow, and step-up reveal grants. It lives in PostgreSQL (see docs/design.md
D11). The source platform scattered this across a database, object storage, and
ticket systems; Sero keeps it in one datastore.

This package holds the schema (``models``); the connection layer and the
repositories that read and write these tables follow. Nothing here stores a
secret value: tokens are recorded by identifier and lifecycle, audit arguments
are stored already redacted, and reveal grants record that access was granted,
not the secret revealed.
"""
