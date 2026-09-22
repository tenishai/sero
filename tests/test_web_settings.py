"""Tests for the backend's settings (D14).

Settings come from the same deployment config the apply layer reads, plus a
small set of environment variables for the things that differ per process
(where the built UI is, which database, the OpenBao token). No secret value is
ever read from the config file.
"""

import pytest

from ladon.config.loader import load_deployment_config_text
from ladon.config.teams import ConfigError
from ladon.web.settings import DEFAULT_IDENTITY_HEADER, parse_web_settings

CONFIG = """
deployment:
  environment: dev
  kv_mount: kv
  openbao:
    address: https://vault.example.com
  ui_dist: /srv/ladon/ui
  reveal:
    enabled: true

identity:
  header: x-forwarded-email
  allow_dev_identity: true

access:
  admins: [admin@example.com]
  members:
    op@example.com: [team-a]
  groups:
    platform: [team-a]
  group_members:
    platform: [lead@example.com]
"""


def test_settings_come_from_the_config():
    settings = parse_web_settings(load_deployment_config_text(CONFIG), env={})
    assert settings.environment == "dev"
    assert settings.identity.header == "x-forwarded-email"
    assert settings.identity.allow_dev_identity is True
    assert settings.openbao_address == "https://vault.example.com"
    assert settings.kv_mount == "kv"
    assert str(settings.ui_dist) == "/srv/ladon/ui"
    assert settings.reveal_enabled is True
    assert settings.access.admins == frozenset({"admin@example.com"})


def test_defaults_when_the_config_says_nothing():
    settings = parse_web_settings(load_deployment_config_text(""), env={})
    assert settings.identity.header == DEFAULT_IDENTITY_HEADER
    assert settings.identity.allow_dev_identity is False
    # Nothing declared means nothing pinned.
    assert settings.environment is None
    assert settings.openbao_address is None
    assert settings.kv_mount == "kv"
    assert settings.ui_dist is None
    # Reveal is off unless a deployment turns it on.
    assert settings.reveal_enabled is False


def test_environment_overrides_the_per_process_paths():
    env = {"LADON_UI_DIST": "/dist", "BAO_ADDR": "https://from-env.example.com"}
    settings = parse_web_settings(load_deployment_config_text(""), env=env)
    assert str(settings.ui_dist) == "/dist"
    assert settings.openbao_address == "https://from-env.example.com"


def test_the_config_may_not_carry_a_token():
    bad = "deployment:\n  openbao:\n    address: https://vault.example.com\n    token: abc\n"
    with pytest.raises(ConfigError, match="token"):
        parse_web_settings(load_deployment_config_text(bad), env={})


def test_a_malformed_access_block_is_rejected():
    with pytest.raises(ConfigError):
        load_deployment_config_text("access: [not, a, mapping]\n")


ASSERTION_CONFIG = """
identity:
  header: x-forwarded-email
  assertion:
    header: x-proxy-assertion
    jwks_url: https://proxy.example.com/keys
    audience: /projects/example/apps/ladon
    issuer: https://proxy.example.com
    email_claim: preferred_username
"""


def test_the_assertion_block_parses():
    settings = parse_web_settings(load_deployment_config_text(ASSERTION_CONFIG), env={})
    assertion = settings.identity.assertion
    assert assertion is not None
    assert assertion.header == "x-proxy-assertion"
    assert assertion.jwks_url == "https://proxy.example.com/keys"
    assert assertion.audience == "/projects/example/apps/ladon"
    assert assertion.issuer == "https://proxy.example.com"
    assert assertion.email_claim == "preferred_username"


def test_no_assertion_block_means_the_plain_header_applies():
    settings = parse_web_settings(load_deployment_config_text(CONFIG), env={})
    assert settings.identity.assertion is None


def test_a_half_configured_assertion_is_rejected():
    # Verifying a signature but not the audience would accept any token the
    # same proxy signed for any other service.
    partial = "identity:\n  assertion:\n    jwks_url: https://proxy.example.com/keys\n"
    with pytest.raises(ConfigError, match="audience"):
        parse_web_settings(load_deployment_config_text(partial), env={})
