"""Tests for the redact hygiene primitive."""

from ladon.hygiene import REDACTED, redact


def test_redacts_by_key_name_case_insensitive():
    out = redact({"password": "hunter2", "Token": "abc", "API_KEY": "x", "name": "team-a"})
    assert out["password"] == REDACTED
    assert out["Token"] == REDACTED
    assert out["API_KEY"] == REDACTED
    assert out["name"] == "team-a"  # benign key preserved


def test_redacts_high_entropy_value_under_benign_key():
    # A synthetic high-entropy value: >=40 chars, token charset, mixed classes.
    # Not shaped like any real cloud credential, to keep the secret scanner quiet.
    secret = "Zx7Qw2Ne8Rt5Yu1Op3As6Df9Gh0Jk4Lm7Bv2Cx5N"
    out = redact({"note": secret, "short": "hello"})
    assert out["note"] == REDACTED
    assert out["short"] == "hello"


def test_leaves_prose_and_slugs_alone():
    out = redact({"description": "a fairly long human sentence about the team namespace"})
    # Spaces break the token pattern, so prose is not treated as a secret.
    assert out["description"].startswith("a fairly long")


def test_walks_nested_dicts_and_lists():
    out = redact(
        {
            "outer": {"password": "p", "ok": "v"},
            "items": [{"secret": "s"}, "plain"],
        }
    )
    assert out["outer"]["password"] == REDACTED
    assert out["outer"]["ok"] == "v"
    assert out["items"][0]["secret"] == REDACTED
    assert out["items"][1] == "plain"


def test_returns_a_copy():
    original = {"password": "p"}
    out = redact(original)
    assert original == {"password": "p"}  # unchanged
    assert out is not original
