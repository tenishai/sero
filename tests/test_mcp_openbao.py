"""Tests for the live OpenBao-backed reader/writer (D12).

Exercised with a fake hvac KV v2 surface, so no live OpenBao is needed. The fake
stores plaintext; the reader must only ever return masked values.
"""

import pytest

from ladon.mcp.openbao import OpenBaoSecrets


class FakeKvV2:
    def __init__(self, store):
        self._store = store  # {path: {key: value}}

    def list_secrets(self, *, path, mount_point):
        # Top-level listing: leaf paths plus one folder marker.
        keys = list(self._store.keys()) + ["sub/"]
        return {"data": {"keys": keys}}

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        if path not in self._store:
            raise KeyError("not found")
        return {"data": {"data": dict(self._store[path]), "metadata": {"version": 3}}}

    def create_or_update_secret(self, *, path, secret, mount_point):
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}


class FakeClient:
    def __init__(self, store):
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": FakeKvV2(store)})()})()


def make_secrets(store, namespaces=("team-a",)):
    client = FakeClient(store)
    return OpenBaoSecrets(
        client_for=lambda ns: client,
        namespaces_for=lambda email: list(namespaces),
    )


def test_list_namespaces_uses_resolver():
    obs = make_secrets({}, namespaces=("team-a", "team-b"))
    assert obs.list_namespaces("op@example.com") == ["team-a", "team-b"]


def test_list_secrets_leaf_and_folder():
    store = {"app": {"API_KEY": "secretval", "DB": "pw"}}
    obs = make_secrets(store)
    paths = obs.list_secrets("op@example.com", "team-a")
    by_path = {p.path: p.key_count for p in paths}
    assert by_path["app"] == 2
    assert "sub/" in by_path  # folder reported, count 0
    assert by_path["sub/"] == 0


def test_get_secret_masks_values():
    store = {"app": {"API_KEY": "supersecretvalue", "DB": "pw"}}
    obs = make_secrets(store)
    detail = obs.get_secret("op@example.com", "team-a", "app", "dev")
    assert detail.exists is True
    assert detail.version == 3
    assert detail.key_count == 2
    # Only masked previews; no plaintext.
    assert "supersecretvalue" not in str(detail.masked_keys)
    assert detail.masked_keys["API_KEY"].startswith("sup")
    assert detail.masked_keys["API_KEY"].endswith("*")
    # Short value fully masked.
    assert set(detail.masked_keys["DB"]) == {"*"}


def test_get_secret_absent_path():
    obs = make_secrets({})
    detail = obs.get_secret("op@example.com", "team-a", "nope", "dev")
    assert detail.exists is False
    assert detail.masked_keys == {}


def test_create_secret_writes_all_keys():
    store = {}
    obs = make_secrets(store)
    result = obs.create_secret(
        "op@example.com", "team-a", "app", "dev", {"API_KEY": "v1", "DB": "v2"}
    )
    assert result.created is True
    assert sorted(result.written_keys) == ["API_KEY", "DB"]
    assert store["app"] == {"API_KEY": "v1", "DB": "v2"}


def test_update_merges_and_removes_preserving_others():
    store = {"app": {"KEEP": "old", "CHANGE": "old", "DROP": "old"}}
    obs = make_secrets(store)
    result = obs.update_secret(
        "op@example.com",
        "team-a",
        "app",
        "dev",
        {"CHANGE": "new", "ADD": "new"},
        ["DROP"],
    )
    assert result.created is False
    assert sorted(result.written_keys) == ["ADD", "CHANGE"]
    assert result.removed_keys == ["DROP"]
    # KEEP untouched, CHANGE updated, ADD added, DROP gone.
    assert store["app"] == {"KEEP": "old", "CHANGE": "new", "ADD": "new"}


def test_update_removed_keys_reports_only_present():
    store = {"app": {"A": "1"}}
    obs = make_secrets(store)
    result = obs.update_secret("op@example.com", "team-a", "app", "dev", {"A": "2"}, ["NOTHERE"])
    # NOTHERE wasn't present, so it is not reported as removed.
    assert result.removed_keys == []


def test_update_on_absent_path_starts_empty():
    store = {}
    obs = make_secrets(store)
    result = obs.update_secret("op@example.com", "team-a", "new", "dev", {"A": "1"}, [])
    assert store["new"] == {"A": "1"}
    assert result.written_keys == ["A"]


def test_update_refuses_to_empty_a_path():
    # Removing the last key would leave an empty secret, which reads as "exists
    # with nothing in it". Deleting the path is a different, explicit operation.
    store = {"app": {"ONLY": "1"}}
    obs = make_secrets(store)
    with pytest.raises(ValueError, match="empty"):
        obs.update_secret("op@example.com", "team-a", "app", "dev", {}, ["ONLY"])
    assert store["app"] == {"ONLY": "1"}


def test_reveal_value_returns_one_plaintext_key():
    store = {"app": {"API_KEY": "dummy-reveal-value", "DB": "dummy-two"}}
    obs = make_secrets(store)
    assert obs.reveal_value("op@example.com", "team-a", "app", "dev", "API_KEY") == (
        "dummy-reveal-value"
    )


def test_reveal_value_unknown_key_is_a_key_error():
    store = {"app": {"API_KEY": "dummy-reveal-value"}}
    obs = make_secrets(store)
    with pytest.raises(KeyError):
        obs.reveal_value("op@example.com", "team-a", "app", "dev", "ABSENT")
