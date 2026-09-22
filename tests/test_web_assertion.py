"""Tests for verifying the proxy's signed assertion (D15).

The key pair, the JWKS document and the token factory come from
tests/fake_assertion.py, so these run offline and nothing here is a real
credential.
"""

from datetime import timedelta

import jwt
import pytest
from starlette.datastructures import Headers

from fake_assertion import (
    AUDIENCE,
    ISSUER,
    JWKS_URL,
    KEY,
    KID,
    OTHER_KEY,
    OTHER_KID,
    SETTINGS,
    jwks,
    token,
    verifier,
)
from ladon.web.assertion import (
    AssertionInvalid,
    AssertionSettings,
    AssertionVerifier,
    JwksCache,
)
from ladon.web.identity import IdentityReader, NoIdentityError

# ── Verification ───────────────────────────────────────────────────────


def test_a_valid_assertion_yields_the_address():
    assert verifier().email(token()) == "op@example.com"


def test_the_wrong_audience_is_refused():
    with pytest.raises(AssertionInvalid):
        verifier().email(token(audience="/projects/example/apps/something-else"))


def test_the_wrong_issuer_is_refused():
    with pytest.raises(AssertionInvalid):
        verifier().email(token(issuer="https://impostor.example.com"))


def test_an_expired_assertion_is_refused():
    with pytest.raises(AssertionInvalid):
        verifier().email(token(expires_in=timedelta(minutes=-5)))


def test_a_signature_from_another_key_is_refused():
    # Signed by a key the proxy does not publish, but naming a kid it does.
    with pytest.raises(AssertionInvalid):
        verifier().email(token(key=OTHER_KEY))


def test_an_unpublished_key_id_is_refused():
    with pytest.raises(AssertionInvalid, match="not published"):
        verifier().email(token(kid="never-published"))


def test_an_unsigned_token_is_refused():
    # alg=none is the classic downgrade; the allow list is what stops it.
    unsigned = jwt.encode(
        {"iss": ISSUER, "aud": AUDIENCE, "email": "op@example.com"},
        key=None,
        algorithm="none",
        headers={"kid": KID},
    )
    with pytest.raises(AssertionInvalid):
        verifier().email(unsigned)


def test_a_token_with_no_expiry_is_refused():
    payload = {"iss": ISSUER, "aud": AUDIENCE, "email": "op@example.com"}
    forever = jwt.encode(payload, KEY, algorithm="ES256", headers={"kid": KID})
    with pytest.raises(AssertionInvalid):
        verifier().email(forever)


def test_a_missing_email_claim_is_refused():
    with pytest.raises(AssertionInvalid, match="email"):
        verifier().email(token(email=None))


def test_a_deployment_can_name_another_claim():
    settings = AssertionSettings(
        jwks_url=JWKS_URL, audience=AUDIENCE, issuer=ISSUER, email_claim="preferred_username"
    )
    document = jwks((KEY, KID))
    subject = AssertionVerifier(settings, fetch=lambda url: document)
    assert subject.email(token(email=None, claims={"preferred_username": "op@example.com"})) == (
        "op@example.com"
    )


def test_garbage_is_refused_without_reaching_the_keys():
    fetches = []

    def fetch(url):
        fetches.append(url)
        return jwks((KEY, KID))

    with pytest.raises(AssertionInvalid, match="well-formed"):
        AssertionVerifier(SETTINGS, fetch=fetch).email("not-a-token")
    assert fetches == []


def test_unreadable_keys_are_refused_not_ignored():
    def fetch(url):
        raise OSError("the key endpoint is unreachable")

    with pytest.raises(AssertionInvalid, match="signing keys"):
        AssertionVerifier(SETTINGS, fetch=fetch).email(token())


# ── The key cache ──────────────────────────────────────────────────────


def test_keys_are_fetched_once_within_the_ttl():
    fetches = []
    clock = [1000.0]
    cache = JwksCache(
        JWKS_URL,
        fetch=lambda url: (fetches.append(url), jwks((KEY, KID)))[1],
        ttl_seconds=900,
        now=lambda: clock[0],
    )
    cache.key_for(KID)
    cache.key_for(KID)
    assert len(fetches) == 1

    clock[0] += 901
    cache.key_for(KID)
    assert len(fetches) == 2


def test_an_unknown_key_id_prompts_one_refetch_for_rotation():
    documents = [jwks((KEY, KID)), jwks((KEY, KID), (OTHER_KEY, OTHER_KID))]
    fetches = []
    clock = [1000.0]

    def fetch(url):
        fetches.append(url)
        return documents[min(len(fetches) - 1, len(documents) - 1)]

    cache = JwksCache(JWKS_URL, fetch=fetch, ttl_seconds=900, now=lambda: clock[0])
    cache.key_for(KID)
    assert len(fetches) == 1

    # The proxy rotated inside the TTL: the new kid is worth one fetch.
    clock[0] += 61
    assert cache.key_for(OTHER_KID) is not None
    assert len(fetches) == 2


def test_a_stream_of_bogus_key_ids_cannot_drive_fetches():
    fetches = []
    clock = [1000.0]
    cache = JwksCache(
        JWKS_URL,
        fetch=lambda url: (fetches.append(url), jwks((KEY, KID)))[1],
        ttl_seconds=900,
        now=lambda: clock[0],
    )
    for _ in range(20):
        with pytest.raises(AssertionInvalid):
            cache.key_for("made-up")
    # The first call fetched; the rate limit swallowed the rest.
    assert len(fetches) == 1


# ── The reader ─────────────────────────────────────────────────────────


def reader(**kwargs):
    return IdentityReader(header="x-email", assertion=verifier(), **kwargs)


def test_the_reader_takes_identity_from_the_assertion():
    headers = Headers({SETTINGS.header: token()})
    assert reader().email(headers) == "op@example.com"


def test_the_plain_header_is_ignored_when_an_assertion_is_configured():
    headers = Headers({SETTINGS.header: token(), "x-email": "someone-else@example.com"})
    assert reader().email(headers) == "op@example.com"


def test_a_missing_assertion_is_no_identity():
    with pytest.raises(NoIdentityError):
        reader().email(Headers({"x-email": "op@example.com"}))


def test_an_invalid_assertion_is_no_identity():
    with pytest.raises(NoIdentityError):
        reader().email(Headers({SETTINGS.header: token(issuer="https://impostor.example.com")}))


def test_the_dev_hatch_cannot_stand_in_for_an_assertion():
    subject = reader(dev_identity="dev@example.com")
    with pytest.raises(NoIdentityError):
        subject.email(Headers({}))


def test_two_assertion_headers_are_refused():
    name = SETTINGS.header.encode()
    headers = Headers(raw=[(name, token().encode()), (name, token().encode())])
    with pytest.raises(NoIdentityError, match="more than one"):
        reader().email(headers)


def test_two_identity_headers_are_refused():
    name = b"x-goog-authenticated-user-email"
    headers = Headers(raw=[(name, b"op@example.com"), (name, b"attacker@example.com")])
    with pytest.raises(NoIdentityError, match="more than one"):
        IdentityReader(header=name.decode()).email(headers)


def test_one_identity_header_is_still_fine():
    name = b"x-goog-authenticated-user-email"
    headers = Headers(raw=[(name, b"op@example.com")])
    assert IdentityReader(header=name.decode()).email(headers) == "op@example.com"
