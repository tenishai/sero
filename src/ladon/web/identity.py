"""The caller's identity, from the proxy in front of the platform (D15).

There is no login form. A proxy authenticates the person and tells the backend
who they are, in one of two ways:

* a plain header naming the address. This is a claim, not a proof: anything that
  can reach the backend's port can set it, so it is only as trustworthy as the
  network path. See the trust boundary in docs/backend.md.
* a signed assertion (``identity.assertion``), which is a proof. When one is
  configured it is required on every request and the plain header is not read at
  all.

Either way the value is normalized once, here, before anything authorizes on it:
an ``<issuer>:`` prefix is stripped and the address is lowercased.

Exactly one instance of the deciding header is accepted. Two mean something
between the caller and this process is adding or rewriting headers, and picking
one of them is how a proxy's value gets replaced by an attacker's.

The dev escape hatch exists so the backend can run locally without a proxy. It
is inert unless the config sets ``identity.allow_dev_identity``, it is inert
whenever an assertion is configured, and switching it on logs a warning at
startup: with it on, anyone who can set an environment variable is anyone.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from ladon.web.assertion import AssertionInvalid, AssertionVerifier
from ladon.web.settings import IdentitySettings

DEV_IDENTITY_ENV = "LADON_DEV_IDENTITY"

log = logging.getLogger(__name__)


class NoIdentityError(Exception):
    """No identity was presented, or what was presented did not verify. 401."""


class MultiHeaders(Protocol):
    """Request headers that can report every value sent under one name."""

    def getlist(self, key: str) -> list[str]: ...


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


def one_header(headers: MultiHeaders, name: str) -> str | None:
    """The single value sent under ``name``, or None. Two is a refusal.

    A duplicated header means something on the path is adding or rewriting
    headers. Taking the first or the last is exactly how a proxy's value gets
    replaced by one an attacker appended.
    """
    values = [v for v in headers.getlist(name) if v.strip()]
    if len(values) > 1:
        raise NoIdentityError(f"more than one {name} header")
    return values[0] if values else None


@dataclass(frozen=True)
class IdentityReader:
    """Reads the caller's email from request headers."""

    header: str
    # The dev hatch is a fallback, never an override: a real header always wins,
    # so turning the hatch on cannot mask the proxy's answer.
    dev_identity: str | None = None
    # When set, the signed assertion is the identity and ``header`` is ignored.
    assertion: AssertionVerifier | None = None

    def email(self, headers: Any) -> str:
        if self.assertion is not None:
            return self._from_assertion(headers)
        raw = one_header(headers, self.header)
        if raw is None:
            if self.dev_identity:
                return self.dev_identity
            raise NoIdentityError("no identity")
        return normalize_email(raw)

    def _from_assertion(self, headers: Any) -> str:
        """Identity from the signed assertion, which is then the only source.

        The plain header is not read here, so whether it is present, absent or
        repeated cannot change the answer. The dev hatch is not read either: a
        deployment that verifies assertions has asked for proof, and an
        environment variable is not one.
        """
        token = one_header(headers, self.assertion.header)
        if token is None:
            raise NoIdentityError("no identity")
        try:
            return normalize_email(self.assertion.email(token))
        except AssertionInvalid as exc:
            raise NoIdentityError("no identity") from exc


def resolve_dev_identity(settings: IdentitySettings, env: dict[str, str] | Any) -> str | None:
    """The dev identity for this process, or None.

    Returns a value only when the config allows the hatch, no assertion is
    configured, and the environment sets one; it warns that identity is
    unverified when it does.
    """
    if not settings.allow_dev_identity:
        return None
    if settings.assertion is not None:
        log.warning(
            "identity.allow_dev_identity is on but identity.assertion is "
            "configured: the hatch is ignored, because a verified assertion is "
            "the identity."
        )
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
