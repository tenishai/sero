"""Secrets-hygiene primitives shared across Ladon.

These enforce rules from CONTRIBUTING.md: refuse placeholder secret values, and
never expose a full secret value (mask to a few characters at most). Keep this
module dependency-free so every layer can rely on it.
"""

from __future__ import annotations

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


class PlaceholderSecretError(ValueError):
    """Raised when a supplied secret value looks like a placeholder."""


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
