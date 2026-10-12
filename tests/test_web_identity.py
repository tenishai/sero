"""Tests for how the backend reads the caller's identity (D15).

Identity comes from a header a trusted proxy sets; there is no login form. The
value may carry an issuer prefix, and casing varies by provider, so it is
normalized before anything authorizes on it.
"""

import logging

import pytest
from starlette.datastructures import Headers

from sero.web.identity import (
    DEV_IDENTITY_ENV,
    IdentityReader,
    NoIdentityError,
    normalize_email,
    resolve_dev_identity,
)
from sero.web.settings import DEFAULT_IDENTITY_HEADER, IdentitySettings


def test_normalize_strips_an_issuer_prefix_and_lowercases():
    assert normalize_email("accounts.example.com:Operator@Example.com") == "operator@example.com"


def test_normalize_leaves_a_bare_address_alone():
    assert normalize_email(" Operator@Example.com ") == "operator@example.com"


def test_normalize_keeps_a_value_whose_colon_is_not_an_issuer_prefix():
    # Only a prefix in front of something that looks like an address is stripped.
    assert normalize_email("not-an-email:either") == "not-an-email:either"


def test_reader_returns_the_header_value():
    reader = IdentityReader(header=DEFAULT_IDENTITY_HEADER)
    headers = Headers({DEFAULT_IDENTITY_HEADER: "accounts.example.com:op@example.com"})
    assert reader.email(headers) == "op@example.com"


def test_reader_refuses_a_missing_header():
    reader = IdentityReader(header=DEFAULT_IDENTITY_HEADER)
    with pytest.raises(NoIdentityError):
        reader.email(Headers({}))


def test_reader_refuses_an_empty_header():
    reader = IdentityReader(header=DEFAULT_IDENTITY_HEADER)
    with pytest.raises(NoIdentityError):
        reader.email(Headers({DEFAULT_IDENTITY_HEADER: "   "}))


def test_dev_identity_is_the_fallback_not_an_override():
    reader = IdentityReader(header=DEFAULT_IDENTITY_HEADER, dev_identity="dev@example.com")
    assert reader.email(Headers({})) == "dev@example.com"
    # A real proxy header still wins, so switching the hatch on cannot mask it.
    assert reader.email(Headers({DEFAULT_IDENTITY_HEADER: "op@example.com"})) == "op@example.com"


def test_dev_identity_is_ignored_unless_the_config_allows_it(caplog):
    env = {DEV_IDENTITY_ENV: "dev@example.com"}
    assert resolve_dev_identity(IdentitySettings(), env) is None


def test_dev_identity_warns_that_identity_is_unverified(caplog):
    env = {DEV_IDENTITY_ENV: "Dev@Example.com"}
    settings = IdentitySettings(allow_dev_identity=True)
    with caplog.at_level(logging.WARNING):
        assert resolve_dev_identity(settings, env) == "dev@example.com"
    assert any(r.levelno == logging.WARNING for r in caplog.records)
    assert "unverified" in caplog.text
