"""Shared validation helpers for the config builders.

The rendering builders (auth, seal, eso, plan, derive) all reject missing or
empty required fields with a ``ConfigError``. Before this module each did it by
hand, in three slightly different dialects; these two helpers give one consistent
message and one place to change it.

``ConfigError`` itself still lives in ``ladon.config.teams`` (where it started and
is widely imported from); these helpers just raise it.
"""

from __future__ import annotations

from collections.abc import Mapping

from ladon.config.teams import ConfigError


def require(value: object, field: str, *, context: str = "") -> None:
    """Raise ConfigError if ``value`` is empty (falsy), naming ``field``.

    ``context`` prefixes the message, e.g. context="gcpckms seal" yields
    "gcpckms seal requires a non-empty 'crypto_key'". Empty strings, empty lists,
    None, and 0 all count as missing; that matches how the builders treat a
    required field.
    """
    if not value:
        prefix = f"{context} " if context else ""
        raise ConfigError(f"{prefix}requires a non-empty {field!r}")


def require_str(value: object, field: str, *, context: str = "") -> str:
    """Require a non-empty ``str`` and return it, else raise ConfigError.

    Stricter than ``require``: a non-string (a number, a list) is rejected too,
    so a mistyped config value fails here rather than downstream.
    """
    if not value or not isinstance(value, str):
        prefix = f"{context} " if context else ""
        raise ConfigError(f"{prefix}requires a non-empty string {field!r}")
    return value


def require_all(params: Mapping, *fields: str, context: str = "") -> dict:
    """Require each named field in ``params`` to be a non-empty string.

    Returns a dict of just those fields, so a builder can validate and pick its
    required inputs in one call. Raises ConfigError on the first missing field,
    using the ``require`` wording ("requires a non-empty 'x'").
    """
    out: dict = {}
    for name in fields:
        value = params.get(name)
        if not value or not isinstance(value, str):
            require(None, name, context=context)  # always raises with the shared message
        out[name] = value
    return out
