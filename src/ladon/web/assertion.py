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
# The floor between two fetches prompted by an unknown key id, so a stream of
# tokens naming key ids that do not exist cannot turn into a stream of requests.
MIN_REFRESH_SECONDS = 60

DEFAULT_ASSERTION_HEADER = "x-goog-iap-jwt-assertion"
DEFAULT_EMAIL_CLAIM = "email"
DEFAULT_FETCH_TIMEOUT = 5

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


def fetch_jwks(url: str) -> dict[str, Any]:
    """Fetch a JWKS document over HTTPS.

    The URL is deployment configuration, never anything a caller supplies.
    """
    with urllib.request.urlopen(url, timeout=DEFAULT_FETCH_TIMEOUT) as response:  # noqa: S310
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

    def key_for(self, kid: str | None) -> Any:
        """The signing key for ``kid``, fetching or refetching as needed."""
        if self._fetched_at is None or (self._now() - self._fetched_at) >= self._ttl:
            self._refresh()
        key = self._lookup(kid)
        if key is None and self._can_refresh_early():
            # A key id we have not seen: the proxy may have rotated inside the
            # TTL. Worth one fetch, rate-limited so bogus ids cannot drive them.
            self._refresh()
            key = self._lookup(kid)
        if key is None:
            raise AssertionInvalid("the assertion names a signing key that is not published")
        return key

    def _lookup(self, kid: str | None) -> Any:
        if kid is not None:
            return self._keys.get(kid)
        # A key set with exactly one key needs no kid to be unambiguous.
        return next(iter(self._keys.values())) if len(self._keys) == 1 else None

    def _can_refresh_early(self) -> bool:
        return self._fetched_at is None or (self._now() - self._fetched_at) >= MIN_REFRESH_SECONDS

    def _refresh(self) -> None:
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
                options={"require": ["exp", "iss", "aud"]},
            )
        except Exception as exc:
            # The reason, never the token: a rejected assertion is still a
            # credential and may be valid somewhere else.
            log.warning("rejected a proxy assertion: %s", type(exc).__name__)
            raise AssertionInvalid(f"the assertion did not verify: {type(exc).__name__}") from exc

        address = claims.get(self._settings.email_claim)
        if not address or not isinstance(address, str):
            raise AssertionInvalid(f"the assertion carries no {self._settings.email_claim!r} claim")
        return address
