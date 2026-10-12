"""A locally signed proxy assertion, for tests.

An EC key pair generated in-process, its public half shaped as a JWKS document,
and a token factory over it. Nothing here is a real credential: the tokens are
signed by a key that exists only for the length of the test run and verify
nowhere else. Shared by the unit tests and the route tests so both agree on what
a valid assertion looks like.
"""

import json
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from sero.web.assertion import AssertionSettings, AssertionVerifier

ISSUER = "https://proxy.example.com"
AUDIENCE = "/projects/example/apps/sero"
JWKS_URL = "https://proxy.example.com/keys"
KID = "key-one"
OTHER_KID = "key-two"

# Generated once: an EC key is cheap, but not free, and every test wants one.
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())


def jwk_for(private_key, kid):
    jwk = json.loads(ECAlgorithm.to_jwk(private_key.public_key()))
    jwk.update({"kid": kid, "alg": "ES256", "use": "sig"})
    return jwk


def jwks(*pairs):
    return {"keys": [jwk_for(key, kid) for key, kid in pairs]}


def token(
    *,
    key=KEY,
    kid=KID,
    audience=AUDIENCE,
    issuer=ISSUER,
    email="op@example.com",
    expires_in=timedelta(minutes=5),
    claims=None,
):
    payload = {
        "iss": issuer,
        "aud": audience,
        "exp": datetime.now(UTC) + expires_in,
        "iat": datetime.now(UTC) - timedelta(seconds=10),
    }
    if email is not None:
        payload["email"] = email
    payload.update(claims or {})
    return jwt.encode(payload, key, algorithm="ES256", headers={"kid": kid})


SETTINGS = AssertionSettings(jwks_url=JWKS_URL, audience=AUDIENCE, issuer=ISSUER)


def verifier(document=None, **kwargs):
    document = document if document is not None else jwks((KEY, KID))
    return AssertionVerifier(SETTINGS, fetch=lambda url: document, **kwargs)
