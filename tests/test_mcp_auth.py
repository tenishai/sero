"""Tests for the MCP OAuth 2.1 token verification (D12).

Tokens are minted in-test with the same HS256 key, so verification runs with no
OAuth server. The allow-list gate runs against in-memory SQLite.
"""

import asyncio
import time

import jwt as pyjwt
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from sero.datastore import repositories as repo
from sero.datastore.models import Base
from sero.mcp.auth import (
    SeroTokenVerifier,
    TokenError,
    verify_access_token,
)

KEY = "test-signing-key-not-a-real-secret"
ISSUER = "https://sero.example/oauth"
AUD = "https://sero.example/mcp"
USER = "op@example.com"


def mint(**overrides):
    now = int(time.time())
    claims = {
        "typ": "access_token",
        "iss": ISSUER,
        "sub": USER,
        "aud": AUD,
        "iat": now,
        "exp": now + 3600,
        "client_id": "sero-cli",
        "scope": "mcp read",
    }
    claims.update(overrides)
    # Drop keys explicitly set to None so we can test missing-claim cases.
    claims = {k: v for k, v in claims.items() if v is not None}
    return pyjwt.encode(claims, KEY, algorithm="HS256")


_DEFAULT_AUDS = [AUD]


def verify(token, *, audiences=_DEFAULT_AUDS):
    return verify_access_token(token, signing_key=KEY, issuer=ISSUER, audiences=audiences)


def test_valid_token_returns_claims():
    claims = verify(mint())
    assert claims.user_email == USER
    assert claims.client_id == "sero-cli"
    assert claims.audience == AUD
    assert claims.scope == "mcp read"


def test_trailing_slash_audience_accepted():
    token = mint(aud=AUD + "/")
    claims = verify(token, audiences=[AUD, AUD + "/"])
    assert claims.audience == AUD + "/"


def test_wrong_signature_rejected():
    token = pyjwt.encode(
        {
            "typ": "access_token",
            "iss": ISSUER,
            "sub": USER,
            "aud": AUD,
            "iat": int(time.time()),
            "exp": int(time.time()) + 60,
            "client_id": "c",
        },
        "a-different-key",
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="invalid access token"):
        verify(token)


def test_wrong_issuer_rejected():
    with pytest.raises(TokenError):
        verify(mint(iss="https://evil.example"))


def test_wrong_audience_rejected():
    with pytest.raises(TokenError):
        verify(mint(aud="https://other.example"))


def test_expired_token_rejected():
    now = int(time.time())
    with pytest.raises(TokenError):
        verify(mint(iat=now - 120, exp=now - 60))


def test_missing_required_claim_rejected():
    with pytest.raises(TokenError):
        verify(mint(client_id=None))


def test_wrong_typ_rejected():
    with pytest.raises(TokenError, match="typ"):
        verify(mint(typ="refresh_token"))


def test_no_audiences_configured_is_error():
    with pytest.raises(TokenError, match="no expected audiences"):
        verify(mint(), audiences=[])


def test_error_never_contains_the_token():
    token = mint(iss="https://evil.example")
    try:
        verify(token)
    except TokenError as exc:
        assert token not in str(exc)


# ── SeroTokenVerifier (allow-list gate) ────────────────────────────────


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(engine)


def make_verifier(session_factory):
    return SeroTokenVerifier(
        signing_key=KEY, issuer=ISSUER, audiences=[AUD], session_factory=session_factory
    )


def test_verifier_accepts_allowed_user(session_factory):
    with session_factory() as s:
        repo.allow(s, email=USER, added_by="admin@example.com")
        s.commit()
    verifier = make_verifier(session_factory)
    token = asyncio.run(verifier.verify_token(mint()))
    assert token is not None
    assert token.user_email == USER
    assert token.scopes == ["mcp", "read"]


def test_verifier_rejects_user_not_on_allow_list(session_factory):
    verifier = make_verifier(session_factory)
    # Valid signature, but the user is not on the allow list.
    assert asyncio.run(verifier.verify_token(mint())) is None


def test_verifier_rejects_invalid_token(session_factory):
    with session_factory() as s:
        repo.allow(s, email=USER, added_by="admin@example.com")
        s.commit()
    verifier = make_verifier(session_factory)
    assert asyncio.run(verifier.verify_token(mint(iss="https://evil.example"))) is None
