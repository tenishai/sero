"""Tests for the shared config validation helpers."""

import pytest

from ladon.config.teams import ConfigError
from ladon.config.validate import require, require_all, require_str


def test_require_passes_on_truthy():
    require("x", "field")  # no raise
    require([1], "field")


@pytest.mark.parametrize("empty", ["", None, [], 0])
def test_require_raises_on_empty(empty):
    with pytest.raises(ConfigError, match="requires a non-empty 'field'"):
        require(empty, "field")


def test_require_context_prefix():
    with pytest.raises(ConfigError, match="gcpckms seal requires a non-empty 'crypto_key'"):
        require("", "crypto_key", context="gcpckms seal")


def test_require_str_returns_value():
    assert require_str("hello", "field") == "hello"


def test_require_str_rejects_non_string():
    with pytest.raises(ConfigError, match="requires a non-empty string 'field'"):
        require_str(42, "field")


def test_require_all_returns_named_fields():
    out = require_all({"a": "1", "b": "2", "c": "3"}, "a", "b", context="ctx")
    assert out == {"a": "1", "b": "2"}


def test_require_all_raises_on_first_missing():
    with pytest.raises(ConfigError, match="ctx requires a non-empty 'b'"):
        require_all({"a": "1", "b": ""}, "a", "b", context="ctx")


def test_require_all_rejects_non_string():
    with pytest.raises(ConfigError, match="requires a non-empty 'n'"):
        require_all({"n": 5}, "n")
