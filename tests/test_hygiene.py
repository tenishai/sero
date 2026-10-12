"""Tests for the secrets-hygiene primitives."""

import pytest

from sero.hygiene import (
    PlaceholderSecretError,
    assert_not_placeholder,
    is_placeholder,
    mask,
)


@pytest.mark.parametrize(
    "value",
    ["", "changeme", "CHANGEME", "  password  ", "TODO", "replace_me", "placeholder"],
)
def test_is_placeholder_detects_placeholders(value):
    assert is_placeholder(value)


@pytest.mark.parametrize("value", ["s3cr3t-real-value", "AKIAEXAMPLEKEY", "a-real-token-123"])
def test_is_placeholder_allows_real_values(value):
    assert not is_placeholder(value)


def test_assert_not_placeholder_raises_on_placeholder():
    with pytest.raises(PlaceholderSecretError):
        assert_not_placeholder("changeme", key="db_password")


def test_assert_not_placeholder_passes_on_real_value():
    # Should not raise.
    assert_not_placeholder("a-real-secret-value", key="db_password")


def test_assert_not_placeholder_error_does_not_leak_value():
    try:
        assert_not_placeholder("changeme", key="api_key")
    except PlaceholderSecretError as err:
        assert "api_key" in str(err)
        assert "changeme" not in str(err)


def test_mask_reveals_only_prefix():
    value = "supersecretvalue"  # 16 chars
    masked = mask(value)
    assert masked == "sup" + "*" * (len(value) - 3)
    assert masked.startswith("sup")
    assert value[3:] not in masked  # the tail never appears


def test_mask_fully_masks_short_values():
    assert mask("ab") == "**"
    assert mask("abc") == "***"


def test_mask_empty_is_empty():
    assert mask("") == ""
