"""Tests for verifying the proxy's signed assertion (D15).

The key pair, the JWKS document and the token factory come from
tests/fake_assertion.py, so these run offline and nothing here is a real
credential.
"""

from datetime import UTC, datetime, timedelta

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
from sero.web.assertion import (
    STALE_KEYS_MAX_SECONDS,
    AssertionInvalid,
    AssertionSettings,
    AssertionVerifier,
    JwksCache,
    fetch_jwks,
)
from sero.web.identity import IdentityReader, NoIdentityError

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


# ── The JWKS URL must be https ─────────────────────────────────────────


def test_a_plaintext_jwks_url_is_refused_at_call_time():
    settings = AssertionSettings(
        jwks_url="http://proxy.example.com/keys", audience=AUDIENCE, issuer=ISSUER
    )
    subject = AssertionVerifier(settings, fetch=lambda url: jwks((KEY, KID)))
    # Configuration is checked when the config is parsed, but the fetcher
    # refuses too: the keys decide who everybody is, and fetching them over a
    # channel anyone can rewrite would let a listener choose the signer.
    with pytest.raises(AssertionInvalid):
        fetch_jwks("http://proxy.example.com/keys")
    assert subject is not None


def test_the_real_fetcher_refuses_a_non_https_url():
    with pytest.raises(AssertionInvalid, match="https"):
        fetch_jwks("http://proxy.example.com/keys")


# ── Clock skew ─────────────────────────────────────────────────────────


def test_a_token_a_few_seconds_early_is_accepted():
    # Proxy and backend clocks differ; a token that becomes valid in ten
    # seconds is skew, not an attack.
    soon = datetime.now(UTC) + timedelta(seconds=10)
    assert verifier().email(token(claims={"nbf": soon})) == "op@example.com"


def test_a_token_far_in_the_future_is_still_refused():
    later = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises(AssertionInvalid):
        verifier().email(token(claims={"nbf": later}, expires_in=timedelta(minutes=20)))


# ── Surviving a key-endpoint outage ────────────────────────────────────


class FlakyKeys:
    """A key endpoint that can be turned off and back on."""

    def __init__(self):
        self.up = True
        self.fetches = 0

    def __call__(self, url):
        self.fetches += 1
        if not self.up:
            raise OSError("the key endpoint is unreachable")
        return jwks((KEY, KID))


def test_a_failing_fetch_is_retried_at_most_once_per_interval():
    keys = FlakyKeys()
    keys.up = False
    clock = [1000.0]
    cache = JwksCache(JWKS_URL, fetch=keys, ttl_seconds=900, now=lambda: clock[0])

    for _ in range(10):
        with pytest.raises(AssertionInvalid):
            cache.key_for(KID)
    # Without stamping the failure, every request would be a fetch, and a
    # struggling endpoint would get a stampede exactly when it is down.
    assert keys.fetches == 1

    clock[0] += 61
    with pytest.raises(AssertionInvalid):
        cache.key_for(KID)
    assert keys.fetches == 2


def test_the_last_good_keys_survive_an_outage_for_a_while():
    keys = FlakyKeys()
    clock = [1000.0]
    subject = AssertionVerifier(SETTINGS, fetch=keys, ttl_seconds=900, now=lambda: clock[0])
    assert subject.email(token()) == "op@example.com"

    # The endpoint goes away. People keep working.
    keys.up = False
    clock[0] += 901
    assert subject.email(token()) == "op@example.com"


def test_keys_do_not_survive_an_outage_forever():
    keys = FlakyKeys()
    clock = [1000.0]
    subject = AssertionVerifier(SETTINGS, fetch=keys, ttl_seconds=900, now=lambda: clock[0])
    subject.email(token())

    keys.up = False
    clock[0] += STALE_KEYS_MAX_SECONDS + 1
    # Past the grace window the keys could have been revoked an hour ago, so
    # refusing is the only honest answer.
    with pytest.raises(AssertionInvalid, match="could not read"):
        subject.email(token())


def test_a_shorter_grace_window_expires_the_keys_sooner():
    # The machine lane asks for one: logging a person out of a UI during a key
    # outage costs more than making a machine retry its provisioning call.
    keys = FlakyKeys()
    clock = [1000.0]
    subject = AssertionVerifier(
        SETTINGS, fetch=keys, ttl_seconds=900, stale_max_seconds=600, now=lambda: clock[0]
    )
    subject.email(token())

    keys.up = False
    # The same 901 seconds that the default hour-long grace rides out, just
    # below, is past this one: the first failed refresh empties the cache.
    clock[0] += 901
    with pytest.raises(AssertionInvalid, match="could not read"):
        subject.email(token())


def test_a_recovered_endpoint_is_used_again():
    keys = FlakyKeys()
    clock = [1000.0]
    subject = AssertionVerifier(SETTINGS, fetch=keys, ttl_seconds=900, now=lambda: clock[0])
    subject.email(token())

    keys.up = False
    clock[0] += 901
    subject.email(token())

    keys.up = True
    clock[0] += 61
    assert subject.email(token()) == "op@example.com"
