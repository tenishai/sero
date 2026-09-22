"""Tests for the redact hygiene primitive."""

from ladon.hygiene import REDACTED, redact, redact_text


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


def test_redact_text_removes_a_token_shaped_word():
    message = "login failed for token AbCdEf0123456789AbCdEf0123456789AbCdEf01 at the mount"
    cleaned = redact_text(message)
    assert "AbCdEf0123456789" not in cleaned
    assert REDACTED in cleaned
    # The sentence still says what happened.
    assert cleaned.startswith("login failed for token ")
    assert cleaned.endswith(" at the mount")


def test_redact_text_leaves_ordinary_prose_alone():
    message = "refusing to leave team-a/app empty; delete the path instead"
    assert redact_text(message) == message


def test_redact_text_removes_credentials_embedded_in_a_url():
    assert "hunter2" not in redact_text("could not reach https://user:hunter2@vault.example.com")


def test_redact_text_handles_no_message():
    assert redact_text("") == ""


def test_strips_credentials_embedded_in_a_url_anywhere():
    # An address is a location, not a credential. One that carries a password
    # must not reach the audit table under any key, benign or not.
    out = redact({"server": "https://user:pw@vault.example:8200", "note": "see the server"})
    assert out["server"] == f"https://{REDACTED}@vault.example:8200"
    assert "pw" not in out["server"]
    assert out["note"] == "see the server"


def test_strips_url_credentials_inside_nested_values():
    out = redact({"args": {"addresses": ["https://user:pw@vault.example"]}})
    assert "pw" not in str(out)
