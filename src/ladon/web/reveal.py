"""The server-side reveal gate (D14).

Revealing is the one action where a plaintext value reaches a browser, so
whether it is possible at all is a deployment's decision, not a user's. The gate
is an interface with a refusing default: an instance that says nothing about
reveal does not reveal. A deployment that wants the feature turns it on in
config, and one that wants friction (a re-auth, an identity-provider step-up)
supplies its own gate without touching the route.

This is the server half of the UI's ``RevealGate``. The UI's gate governs the
prompt; this one governs the answer.
"""

from __future__ import annotations

from typing import Protocol

# The single message a refusing deployment gives back, so the UI can recognize
# "this deployment does not do that" rather than "you may not".
NOT_ENABLED = "reveal is not enabled in this deployment"


class RevealDeniedError(Exception):
    """Revealing is not permitted. The route answers 403 with this message."""


class RevealGate(Protocol):
    """Decides whether this caller may reveal a plaintext value right now."""

    def require(self, email: str) -> None:
        """Return quietly when permitted; raise ``RevealDeniedError`` otherwise."""
        ...


class DenyGate:
    """The default: this deployment does not reveal values to anyone."""

    def require(self, email: str) -> None:
        raise RevealDeniedError(NOT_ENABLED)


class AllowGate:
    """Reveal with no extra step, for a deployment that has chosen that.

    Access is still gated by the allow list and namespace membership on the
    route; this gate only answers whether the deployment permits reveal at all.
    """

    def require(self, email: str) -> None:
        return None
