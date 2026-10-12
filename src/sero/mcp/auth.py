"""OAuth 2.1 bearer-token verification for the MCP surface (D12).

The MCP server authenticates every request with a bearer access token. This
module verifies that token in two layers:

* ``verify_access_token`` is pure: it validates a JWT's signature, issuer,
  audience, expiry, and required claims, and returns typed ``AccessTokenClaims``.
  It has no live dependency, so it is tested by minting a token with the same key.
* ``SeroTokenVerifier`` implements the MCP ``TokenVerifier`` interface. It calls
  ``verify_access_token`` and then checks the datastore allow list, so a removed
  allow-list entry revokes access without redeploying. It returns a token
  carrying ``user_email`` for the tools to act as the signed-in user.

The access token is minted by an OAuth server (a later piece) after the user
signs in with the deployment's identity provider. Tokens are HS256-signed with a
key the server holds; the key is a secret supplied out of band, never committed.
"""

from __future__ import annotations

from dataclasses import dataclass

import jwt as pyjwt
from mcp.server.auth.provider import AccessToken, TokenVerifier
from sqlalchemy.orm import Session, sessionmaker

from sero.datastore import repositories as repo

JWT_ALGORITHM = "HS256"
ACCESS_TOKEN_TYPE = "access_token"

# Claims an access token must carry to be considered valid.
_REQUIRED_CLAIMS = ["typ", "iss", "sub", "aud", "iat", "exp", "client_id"]


class TokenError(Exception):
    """A presented access token is invalid, expired, or not permitted."""


@dataclass(frozen=True)
class AccessTokenClaims:
    """The verified claims of an access token."""

    user_email: str
    client_id: str
    audience: str
    scope: str
    expires_at: int


def verify_access_token(
    token: str,
    *,
    signing_key: str,
    issuer: str,
    audiences: list[str],
) -> AccessTokenClaims:
    """Verify a bearer access token and return its claims.

    Validates the HS256 signature against ``signing_key``, the ``issuer``, the
    ``audience`` (any of ``audiences`` is accepted, since MCP canonical-URI rules
    let clients send the trailing-slash form or not), the expiry, and the required
    claims, and confirms ``typ == "access_token"``. Raises ``TokenError`` on any
    failure; the message never includes the token.
    """
    if not audiences:
        raise TokenError("no expected audiences configured")

    last_err: Exception | None = None
    for aud in audiences:
        try:
            payload = pyjwt.decode(
                token,
                signing_key,
                algorithms=[JWT_ALGORITHM],
                issuer=issuer,
                audience=aud,
                options={"require": _REQUIRED_CLAIMS},
            )
        except pyjwt.InvalidAudienceError as exc:
            last_err = exc
            continue
        except pyjwt.PyJWTError as exc:
            raise TokenError(f"invalid access token: {exc}") from exc

        if payload.get("typ") != ACCESS_TOKEN_TYPE:
            raise TokenError(f"expected typ={ACCESS_TOKEN_TYPE!r}, got {payload.get('typ')!r}")
        return AccessTokenClaims(
            user_email=payload["sub"],
            client_id=payload["client_id"],
            audience=payload["aud"],
            scope=payload.get("scope", ""),
            expires_at=int(payload["exp"]),
        )

    # Every audience raised InvalidAudienceError.
    raise TokenError(f"invalid access token: {last_err}")


class SeroAccessToken(AccessToken):
    """An MCP AccessToken carrying the signed-in user's email for the tools."""

    user_email: str


class SeroTokenVerifier(TokenVerifier):
    """Verify bearer tokens and gate on the datastore allow list.

    The signature/issuer/audience/expiry checks are the pure
    ``verify_access_token``; on top, the user must be on the allow list, checked
    per request against the datastore so a removed entry revokes access without a
    redeploy. Returns ``None`` for any rejection, as the MCP interface expects.
    """

    def __init__(
        self,
        *,
        signing_key: str,
        issuer: str,
        audiences: list[str],
        session_factory: sessionmaker[Session],
    ) -> None:
        self._signing_key = signing_key
        self._issuer = issuer
        self._audiences = audiences
        self._session_factory = session_factory

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            claims = verify_access_token(
                token,
                signing_key=self._signing_key,
                issuer=self._issuer,
                audiences=self._audiences,
            )
        except TokenError:
            return None

        with self._session_factory() as session:
            if not repo.is_allowed(session, claims.user_email):
                return None

        return SeroAccessToken(
            token=token,
            client_id=claims.client_id,
            scopes=[s for s in claims.scope.split() if s],
            expires_at=claims.expires_at,
            resource=claims.audience,
            user_email=claims.user_email,
        )
