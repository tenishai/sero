"""The agent (MCP) surface (D12): selected operations as MCP tools.

Sero exposes a small, curated set of operations to tool-enabled agents over the
Model Context Protocol, so a Claude can list and inspect a user's namespaces and
secrets (and, in later slices, create and update them) on the user's behalf. The
server is built on FastMCP with OAuth 2.1 bearer auth.

Secrets hygiene is built in, not bolted on: reads return masked key names only
(never a value), every call is audited through the datastore, and access is gated
by the allow list and per-namespace authorization. The tools compose the existing
config, apply, and datastore layers; they do not reimplement them.

This package holds the read port (``ports``) the tools read through, and the
server and tools (``server``). The read port is an interface so the tools are
testable with a fake and the live OpenBao-backed implementation is separable.
"""
