"""Tests for the live OpenBao-backed reader/writer (D12).

Exercised with a fake hvac KV v2 surface, so no live OpenBao is needed. The fake
stores plaintext; the reader must only ever return masked values.
"""

import pytest

from ladon.mcp.openbao import OpenBaoSecrets, SecretNotDeleted

# What the store says about when the current version was written.
WRITTEN_AT = "2026-09-22T10:00:00.000000Z"


class InvalidPath(Exception):
    """Named like hvac's: an empty KV listing is a 404 from OpenBao."""


class Forbidden(Exception):
    """A denial from OpenBao. Shaped like hvac's, with its status code."""

    status_code = 403


class FakeKvV2:
    def __init__(self, store):
        self._store = store  # {path: {key: value}}
        self.listed: list[str] = []
        self.destroyed: list[str] = []

    def list_secrets(self, *, path, mount_point):
        # The way KV v2 answers: the distinct next segment under the prefix,
        # folders with a trailing slash, and a 404 when nothing is there.
        self.listed.append(path)
        prefix = path.strip("/") + "/" if path.strip("/") else ""
        keys = sorted(
            {
                rest.split("/")[0] + ("/" if "/" in rest else "")
                for key in self._store
                if key.startswith(prefix)
                for rest in [key[len(prefix) :]]
            }
        )
        if not keys:
            raise InvalidPath(f"no keys under {path!r}")
        return {"data": {"keys": keys}}

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        if path not in self._store:
            # What hvac raises for a 404, which is the only failure that means
            # the path is not there.
            raise InvalidPath(f"no secret at {path!r}")
        return {
            "data": {
                "data": dict(self._store[path]),
                "metadata": {"version": 3, "created_time": WRITTEN_AT},
            }
        }

    def create_or_update_secret(self, *, path, secret, mount_point):
        self._store[path] = dict(secret)
        return {"data": {"version": 1}}

    def delete_metadata_and_all_versions(self, *, path, mount_point):
        # The real call removes the metadata and every version with it, which
        # is what makes the path gone rather than soft-deleted.
        self.destroyed.append(path)
        self._store.pop(path, None)


class FakeClient:
    def __init__(self, store):
        self.kv = FakeKvV2(store)
        self.secrets = type("S", (), {"kv": type("K", (), {"v2": self.kv})()})()


def make_secrets(store, namespaces=("team-a",)):
    client = FakeClient(store)
    return OpenBaoSecrets(
        client_for=lambda ns: client,
        namespaces_for=lambda email: list(namespaces),
    )


def make_secrets_and_client(store, namespaces=("team-a",)):
    client = FakeClient(store)
    secrets = OpenBaoSecrets(
        client_for=lambda ns: client,
        namespaces_for=lambda email: list(namespaces),
    )
    return secrets, client


def test_list_namespaces_uses_resolver():
    obs = make_secrets({}, namespaces=("team-a", "team-b"))
    assert obs.list_namespaces("op@example.com") == ["team-a", "team-b"]


def test_list_secrets_walks_folders_to_the_leaves():
    # A folder is not a secret. The listing follows it down and reports the
    # leaves by their full path, so `apps/sample/demo` shows as itself and
    # not as an `apps/` entry with a count of zero.
    store = {
        "app": {"API_KEY": "secretval", "DB": "pw"},
        "apps/sample/demo": {"USER": "u"},
        "apps/sample/cabal": {"TOKEN": "t", "URL": "x"},
    }
    obs = make_secrets(store)
    paths = obs.list_secrets("op@example.com", "team-a")
    by_path = {p.path: p.key_count for p in paths}
    assert by_path == {"app": 2, "apps/sample/cabal": 2, "apps/sample/demo": 1}
    assert not any(p.path.endswith("/") for p in paths)


def test_list_secrets_on_an_empty_mount_is_an_empty_list():
    # OpenBao answers an empty KV listing with a 404; a namespace with no
    # secrets yet is an ordinary state, not an internal error.
    obs = make_secrets({})
    assert obs.list_secrets("op@example.com", "team-a") == []


def test_list_secrets_stops_at_the_depth_bound():
    deep = "/".join(["d"] * 12)
    store = {deep: {"K": "v"}, "shallow": {"K": "v"}}
    obs = make_secrets(store)
    paths = obs.list_secrets("op@example.com", "team-a")
    assert [p.path for p in paths] == ["shallow"]


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


def test_get_secret_reports_when_the_current_version_was_written():
    obs = make_secrets({"app": {"API_KEY": "supersecretvalue"}})
    detail = obs.get_secret("op@example.com", "team-a", "app", "dev")
    assert detail.updated_at == WRITTEN_AT


def test_get_secret_reports_no_timestamp_when_the_store_gives_none():
    class NoTimes(FakeKvV2):
        def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
            return {"data": {"data": {"API_KEY": "v"}, "metadata": {"version": 1}}}

    client = FakeClient({})
    client.secrets.kv.v2 = NoTimes({})
    obs = OpenBaoSecrets(client_for=lambda ns: client, namespaces_for=lambda email: ["team-a"])
    assert obs.get_secret("op@example.com", "team-a", "app", "dev").updated_at is None


def test_delete_secret_removes_the_metadata_and_every_version():
    store = {"app": {"API_KEY": "supersecretvalue"}}
    obs, client = make_secrets_and_client(store)
    obs.delete_secret("op@example.com", "team-a", "app")
    # The metadata call, not a soft delete: a soft-deleted path still exists.
    assert client.kv.destroyed == ["app"]
    assert store == {}


def test_deleting_a_path_that_is_not_there_is_not_an_error():
    # Unconditional: OpenBao answers a metadata delete the same either way, so
    # reading first to decide would only add a race.
    obs, client = make_secrets_and_client({"app": {"API_KEY": "v"}})
    obs.delete_secret("op@example.com", "team-a", "nope")
    assert client.kv.destroyed == ["nope"]


def test_deleting_twice_is_not_an_error():
    obs, client = make_secrets_and_client({"app": {"API_KEY": "v"}})
    obs.delete_secret("op@example.com", "team-a", "app")
    obs.delete_secret("op@example.com", "team-a", "app")
    assert client.kv.destroyed == ["app", "app"]


def test_a_delete_that_leaves_the_path_readable_raises():
    # The failure worth catching here rather than the next time somebody reads
    # the path and finds the credential still live.
    class KeepsIt(FakeKvV2):
        def delete_metadata_and_all_versions(self, *, path, mount_point):
            self.destroyed.append(path)

    client = FakeClient({"app": {"API_KEY": "v"}})
    client.secrets.kv.v2 = KeepsIt({"app": {"API_KEY": "v"}})
    obs = OpenBaoSecrets(client_for=lambda ns: client, namespaces_for=lambda email: ["team-a"])
    with pytest.raises(SecretNotDeleted, match="still readable"):
        obs.delete_secret("op@example.com", "team-a", "app")


# Only a 404 is absence ---------------------------------------------------------


class Denies(FakeKvV2):
    """A store that refuses the read. Not a 404: Ladon could not look."""

    def read_secret_version(self, *, path, mount_point, raise_on_deleted_version=False):
        raise Forbidden("permission denied")


def denying_secrets():
    client = FakeClient({})
    client.secrets.kv.v2 = Denies({})
    return OpenBaoSecrets(client_for=lambda ns: client, namespaces_for=lambda email: ["team-a"])


def test_a_denied_read_is_raised_not_reported_as_absent():
    # Reporting "I could not look" as "there is nothing there" is how a read
    # answers 404 for a secret that exists.
    with pytest.raises(Forbidden):
        denying_secrets().get_secret("op@example.com", "team-a", "app", "dev")


def test_a_denied_read_stops_a_delete_before_the_read_back():
    with pytest.raises(Forbidden):
        denying_secrets().delete_secret("op@example.com", "team-a", "app")


def test_a_denied_read_stops_an_update_rather_than_overwriting():
    # An update merges into what the read returns and writes the result back,
    # so a swallowed denial would merge into nothing and destroy every key.
    class DeniesRead(Denies):
        pass

    store = {"app": {"KEEP": "old"}}
    client = FakeClient(store)
    client.secrets.kv.v2 = DeniesRead(store)
    obs = OpenBaoSecrets(client_for=lambda ns: client, namespaces_for=lambda email: ["team-a"])
    with pytest.raises(Forbidden):
        obs.update_secret("op@example.com", "team-a", "app", "dev", {"ADD": "new"}, [])
    assert store == {"app": {"KEEP": "old"}}


def test_a_denied_listing_is_raised_not_reported_as_empty():
    class DeniesList(FakeKvV2):
        def list_secrets(self, *, path, mount_point):
            raise Forbidden("permission denied")

    client = FakeClient({})
    client.secrets.kv.v2 = DeniesList({})
    obs = OpenBaoSecrets(client_for=lambda ns: client, namespaces_for=lambda email: ["team-a"])
    with pytest.raises(Forbidden):
        obs.list_secrets("op@example.com", "team-a")
