"""Secrets-hygiene primitives shared across Ladon.

These enforce rules from CONTRIBUTING.md: refuse placeholder secret values, never
expose a full secret value (mask to a few characters at most), and redact secret
material out of structured data before it is stored or logged (the audit trail).
Keep this module dependency-free so every layer can rely on it.

:class:`WriteRefused` lives here for the same reason: it is the one exception
type that says "we decided not to", and every layer needs to be able to raise it
without reaching sideways for a definition.
"""

from __future__ import annotations

import re
from typing import Any

# Values that almost always indicate a mistake rather than a real secret.
# Compared case-insensitively after stripping surrounding whitespace.
_PLACEHOLDERS = frozenset(
    {
        "",
        "changeme",
        "change-me",
        "change_me",
        "password",
        "passwd",
        "secret",
        "todo",
        "tbd",
        "replace_me",
        "replaceme",
        "replace-me",
        "xxx",
        "example",
        "placeholder",
    }
)


# Credentials embedded in a URL ("https://user:pass@host/..."). No URL Ladon
# handles should carry them: an address is a location, not a credential, and one
# that carries a password must never be printed, rendered, or stored.
URL_USERINFO = re.compile(r"//[^/@\s]*:[^/@\s]*@")


class WriteRefused(ValueError):
    """A write Ladon will not perform, for a reason the caller can act on.

    Distinct from something going wrong: a refusal is a decision, and the
    surfaces above answer it with "you asked for something we do not do" rather
    than "we broke". The message names the key, path, or field at fault and
    never a value.

    A ``ValueError`` subclass so callers that predate it keep working.
    """


class PlaceholderSecretError(WriteRefused):
    """Raised when a supplied secret value looks like a placeholder."""


def has_url_userinfo(value: str) -> bool:
    """Return True if a URL carries embedded credentials."""
    return bool(URL_USERINFO.search(value))


def strip_url_userinfo(value: str) -> str:
    """Remove embedded credentials from a URL, leaving the rest intact."""
    return URL_USERINFO.sub("//", value)


def is_placeholder(value: str) -> bool:
    """Return True if value looks like a placeholder, not a real secret."""
    return value.strip().lower() in _PLACEHOLDERS


def assert_not_placeholder(value: str, *, key: str = "value") -> None:
    """Raise PlaceholderSecretError if value is a placeholder.

    Use before writing any secret. The error message never includes the value.
    """
    if is_placeholder(value):
        raise PlaceholderSecretError(
            f"refusing placeholder value for {key!r}; supply a real secret"
        )


def mask(value: str, *, shown: int = 3) -> str:
    """Mask a secret value, revealing at most the first ``shown`` characters.

    Short values are fully masked so nothing meaningful leaks. Use this anywhere
    a value might otherwise reach a log or a response; never print the raw value.
    """
    if not value:
        return ""
    if len(value) <= shown:
        return "*" * len(value)
    return value[:shown] + "*" * (len(value) - shown)


REDACTED = "<redacted>"

# Keys whose values are secret material and are always redacted, regardless of
# the value's shape. Matched case-insensitively.
_REDACT_KEYS = frozenset(
    {
        "data",
        "secret_values",
        "secret_keys",
        "password",
        "passwd",
        "value",
        "values",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "key",
        "api_key",
        "apikey",
        "private_key",
        "client_secret",
        "secret",
    }
)

# A value that "looks like a secret" even under a benign key: long and drawn from
# the base64/token character set, with mixed character classes (so prose and
# slugs are left alone).
_HIGH_ENTROPY_LEN = 40
_HIGH_ENTROPY_PATTERN = re.compile(r"^[A-Za-z0-9_\-+/=]+$")


def _looks_high_entropy(s: str) -> bool:
    if len(s) < _HIGH_ENTROPY_LEN or not _HIGH_ENTROPY_PATTERN.match(s):
        return False
    has_upper = any(c.isupper() for c in s)
    has_lower = any(c.islower() for c in s)
    has_digit = any(c.isdigit() for c in s)
    return (has_upper and has_lower) or (has_digit and (has_upper or has_lower))


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return REDACTED if _looks_high_entropy(value) else value
    if isinstance(value, dict):
        return redact(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def redact(args: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``args`` with secret material removed.

    A value under a key in ``_REDACT_KEYS`` (matched case-insensitively) is
    replaced with ``"<redacted>"``; any string value that looks high-entropy is
    also redacted, even under a benign key. Nested dicts and lists are walked.
    Use before storing arguments in the audit trail or otherwise logging them, so
    a secret never reaches durable state.
    """
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(key, str) and key.lower() in _REDACT_KEYS:
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out
