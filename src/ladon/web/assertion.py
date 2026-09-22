"""Verifying the proxy's signed assertion (D15).

A plain identity header is a claim, not a proof. Anything that can reach the
backend's port can set it, so the header is only as good as the promise that
nothing else can reach that port. Proxies that authenticate people generally
also sign a short-lived JWT asserting who they authenticated; when a deployment
configures one, that assertion becomes the identity and the plain header is not
read at all.

Provider-neutral by construction: the header name, the JWKS URL, the expected
issuer and audience, and which claim carries the address are all configuration.
Nothing here knows which proxy is in front.

Verification is strict. The signature must be ES256 or RS256 over a key
currently published at the JWKS URL, and the issuer, audience and expiry must
all match. Anything else is no identity at all, which the routes answer 401.
Failures are logged without the token: a rejected assertion is still a bearer
credential.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import PyJWKSet

# The only signature algorithms accepted. Naming them (rather than trusting the
# token's own header) is what stops an attacker downgrading to "none" or to a
# symmetric algorithm keyed on the public key.
ALLOWED_ALGORITHMS = ("ES256", "RS256")

# How long a fetched key set is trusted before it is fetched again.
DEFAULT_JWKS_TTL_SECONDS = 900
# The floor between two fetch attempts, whether the last one found an unknown
# key id or failed outright. Without it a stream of tokens naming key ids that
# do not exist, or an endpoint that is down, becomes a fetch per request: a
# stampede aimed at the endpoint exactly when it is least able to answer.
MIN_REFRESH_SECONDS = 60
# How long the last key set that did arrive keeps being used once refreshes
# start failing. A key endpoint outage should not log everybody out, but a key
# revoked during the outage has to stop working eventually, so the window is
# bounded rather than indefinite.
STALE_KEYS_MAX_SECONDS = 3600

# Tolerance for clock difference between the proxy and this process, applied to
# exp and nbf. Small: these assertions live minutes, not hours.
CLOCK_SKEW_LEEWAY_SECONDS = 30

DEFAULT_ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
DEFAULT_EMAIL_CLAIM = "email"
DEFAULT_FETCH_TIMEOUT = 5

# Claims a token must carry to be considered at all. A token with no expiry is
# a permanent credential; one with no issuer or audience is a token for
# somewhere else that happens to verify here. A caller that needs more (the
# machine lane pins a subject, so it requires ``sub``) says so in its settings.
DEFAULT_REQUIRED_CLAIMS = ("exp", "iss", "aud")

log = logging.getLogger(__name__)

# url -> the parsed JWKS document.
JwksFetcher = Callable[[str], dict[str, Any]]


class AssertionInvalid(Exception):
    """The assertion was absent, malformed, or did not verify."""


@dataclass(frozen=True)
class AssertionSettings:
    """Where the assertion comes from and what it must say."""

    jwks_url: str
    audience: str
    issuer: str
    header: str = DEFAULT_ASSERTION_HEADER
    email_claim: str = DEFAULT_EMAIL_CLAIM
    # Claims whose absence makes the token unacceptable, whatever else verifies.
    required_claims: tuple[str, ...] = DEFAULT_REQUIRED_CLAIMS


def require_https(url: str) -> None:
    """Refuse a key-set URL that is not HTTPS.

    These keys decide who everybody is. Fetching them over a channel anyone on
    the path can rewrite would let a listener choose the signer, which is the
    whole point of verifying the assertion.
    """
    if not url.lower().startswith("https://"):
        raise AssertionInvalid("the signing-key URL must be https")


def fetch_jwks(url: str) -> dict[str, Any]:
    """Fetch a JWKS document over HTTPS.

    The URL is deployment configuration, never anything a caller supplies, and
    the scheme is checked here as well as at parse time so no caller can reach
    urlopen with something else.
    """
    require_https(url)
    with urllib.request.urlopen(url, timeout=DEFAULT_FETCH_TIMEOUT) as response:
        return json.loads(response.read())


class JwksCache:
    """The proxy's published keys, refetched on a timer and on key rotation."""

    def __init__(
        self,
        url: str,
        *,
        fetch: JwksFetcher = fetch_jwks,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._url = url
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._now = now
        self._keys: dict[str, Any] = {}
        self._fetched_at: float | None = None
        self._attempted_at: float | None = None

    def key_for(self, kid: str | None) -> Any:
        """The signing key for ``kid``, fetching or refetching as needed.

        A refresh that fails does not empty the cache: the last key set that did
        arrive keeps working for ``STALE_KEYS_MAX_SECONDS``, so an outage at the
        key endpoint does not log everybody out. Past that window there are no
        usable keys and every assertion is refused.
        """
        if self._stale():
            self._try_refresh()
        key = self._lookup(kid)
        if key is None and self._can_attempt():
            # A key id we have not seen: the proxy may have rotated inside the
            # TTL. Worth one fetch, rate-limited so bogus ids cannot drive them.
            self._try_refresh()
            key = self._lookup(kid)
        if key is None:
            raise AssertionInvalid("the assertion names a signing key that is not published")
        return key

    def _stale(self) -> bool:
        return self._fetched_at is None or (self._now() - self._fetched_at) >= self._ttl

    def _try_refresh(self) -> None:
        """Refresh if the rate limit allows, keeping usable keys on failure."""
        if not self._can_attempt():
            self._expire_if_too_stale()
            return
        try:
            self._refresh()
        except AssertionInvalid:
            # Hold on to what we have, within the grace window. Past it the keys
            # could have been revoked an hour ago, so they stop being an answer.
            self._expire_if_too_stale()
            if not self._keys:
                raise

    def _expire_if_too_stale(self) -> None:
        if self._fetched_at is None:
            return
        if (self._now() - self._fetched_at) >= STALE_KEYS_MAX_SECONDS:
            self._keys = {}

    def _can_attempt(self) -> bool:
        """Whether enough time has passed since the last fetch attempt."""
        last = self._attempted_at
        return last is None or (self._now() - last) >= MIN_REFRESH_SECONDS

    def _lookup(self, kid: str | None) -> Any:
        if kid is not None:
            return self._keys.get(kid)
        # A key set with exactly one key needs no kid to be unambiguous.
        return next(iter(self._keys.values())) if len(self._keys) == 1 else None

    def _refresh(self) -> None:
        # Stamped before the attempt, not after it, so a failure counts against
        # the rate limit exactly as a success does.
        self._attempted_at = self._now()
        try:
            document = self._fetch(self._url)
            key_set = PyJWKSet.from_dict(document)
        except Exception as exc:
            # Never include the response body: a misconfigured URL could point
            # at something that is not a key set at all.
            raise AssertionInvalid(
                f"could not read the signing keys: {type(exc).__name__}"
            ) from exc
        self._keys = {key.key_id: key.key for key in key_set.keys if key.key_id}
        if len(key_set.keys) == 1 and not self._keys:
            self._keys = {"": key_set.keys[0].key}
        self._fetched_at = self._now()


class AssertionVerifier:
    """Turns a signed assertion header into a verified email address."""

    def __init__(
        self,
        settings: AssertionSettings,
        *,
        fetch: JwksFetcher = fetch_jwks,
        ttl_seconds: int = DEFAULT_JWKS_TTL_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._jwks = JwksCache(settings.jwks_url, fetch=fetch, ttl_seconds=ttl_seconds, now=now)

    @property
    def header(self) -> str:
        return self._settings.header

    def email(self, token: str) -> str:
        """Verify ``token`` and return the address it asserts.

        Raises :class:`AssertionInvalid` for anything that does not verify, so a
        forged, expired, misaddressed or unsigned assertion is indistinguishable
        from no identity at all.
        """
        claims = self.claims(token)
        address = claims.get(self._settings.email_claim)
        if not address or not isinstance(address, str):
            raise AssertionInvalid(f"the assertion carries no {self._settings.email_claim!r} claim")
        return address

    def claims(self, token: str) -> dict[str, Any]:
        """Verify ``token`` and return every claim it carries.

        The verification is the same one :meth:`email` rests on: an allow-listed
        algorithm, a key currently published at the JWKS URL, and a matching
        issuer, audience and expiry. Callers that authorize on a claim other
        than the address (the machine lane pins ``sub``) read it from here, so
        there is one verification and not two.
        """
        try:
            kid = jwt.get_unverified_header(token).get("kid")
        except Exception as exc:
            raise AssertionInvalid("the assertion is not a well-formed token") from exc

        key = self._jwks.key_for(kid)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self._settings.audience,
                issuer=self._settings.issuer,
                options={"require": list(self._settings.required_claims)},
                # The proxy's clock and ours are not the same clock. A few
                # seconds either side is skew, not an attack; an assertion this
                # short-lived gains nothing meaningful from the slack.
                leeway=CLOCK_SKEW_LEEWAY_SECONDS,
            )
        except Exception as exc:
            # The reason, never the token: a rejected assertion is still a
            # credential and may be valid somewhere else.
            log.warning("rejected a proxy assertion: %s", type(exc).__name__)
            raise AssertionInvalid(f"the assertion did not verify: {type(exc).__name__}") from exc
        return dict(claims)
