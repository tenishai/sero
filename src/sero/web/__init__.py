"""The Sero HTTP backend (D14).

One long-running process serves the API the UI calls, the built UI itself, and
(later) the apply operations as background jobs. The tool logic is not duplicated
here: the routes call the same ``*_impl`` functions the MCP surface calls, so the
allow list, the namespace gate, the merge rules, placeholder refusal, and the
audit trail have exactly one implementation.
"""
