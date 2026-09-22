"""The caller's identity, from a trusted proxy header (D15).

There is no login form. A proxy in front of the platform authenticates the person
and passes the result in a header whose name the deployment configures. The value
may carry an issuer prefix (``<issuer>:<email>``) and its casing varies by
provider, so it is normalized once, here, before anything authorizes on it.

The dev escape hatch exists so the backend can be run locally without a proxy. It
is inert unless the config sets ``identity.allow_dev_identity``, and switching it
on logs a warning at startup: with it on, anyone who can set an environment
variable is anyone.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass

from ladon.web.settings import IdentitySettings

DEV_IDENTITY_ENV = "LADON_DEV_IDENTITY"

log = logging.getLogger(__name__)


class NoIdentityError(Exception):
    """No identity was presented. The route answers 401."""


def normalize_email(raw: str) -> str:
    """Normalize a header value to a bare, lowercased email address.

    Strips an ``<issuer>:`` prefix when what follows looks like an address, so a
    value that merely contains a colon is left intact rather than truncated.
    """
    value = raw.strip()
    if ":" in value:
        candidate = value.rsplit(":", 1)[-1].strip()
        if "@" in candidate:
            value = candidate
    return value.lower()


@dataclass(frozen=True)
class IdentityReader:
    """Reads the caller's email from request headers."""

    header: str
    # The dev hatch is a fallback, never an override: a real header always wins,
    # so turning the hatch on cannot mask the proxy's answer.
    dev_identity: str | None = None

    def email(self, headers: Mapping[str, str]) -> str:
        raw = headers.get(self.header) or ""
        if not raw.strip():
            if self.dev_identity:
                return self.dev_identity
            raise NoIdentityError("no identity")
        return normalize_email(raw)


def resolve_dev_identity(settings: IdentitySettings, env: Mapping[str, str]) -> str | None:
    """The dev identity for this process, or None.

    Returns a value only when the config allows the hatch and the environment
    sets one, and warns that identity is unverified when it does.
    """
    if not settings.allow_dev_identity:
        return None
    raw = env.get(DEV_IDENTITY_ENV)
    if not raw or not raw.strip():
        return None
    email = normalize_email(raw)
    log.warning(
        "%s is set and identity.allow_dev_identity is on: every request is "
        "attributed to %s and identity is unverified. Never run a shared or "
        "deployed instance this way.",
        DEV_IDENTITY_ENV,
        email,
    )
    return email
