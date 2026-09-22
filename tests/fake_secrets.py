"""A stand-in for the secrets implementation, complete enough to start.

``create_app`` refuses an implementation that cannot serve every route the
deployment exposes, so that a missing method is a boot failure naming it
rather than a 500 on the first caller who reaches that route. A test that
exercises one route still has to hand over something whole, and this is it.

Every method raises, naming itself. A test overrides the ones it means to
exercise, and anything else being called is a failure that says which.
"""

from __future__ import annotations

from typing import Any


class StubSecrets:
    """The whole port surface, with nothing behind it."""

    def _unused(self, name: str) -> Any:
        raise NotImplementedError(f"this test did not expect {name} to be called")

    def list_namespaces(self, user_email: str) -> Any:
        return self._unused("list_namespaces")

    def list_secrets(self, user_email: str, namespace: str) -> Any:
        return self._unused("list_secrets")

    def get_secret(self, user_email: str, namespace: str, path: str, env: str) -> Any:
        return self._unused("get_secret")

    def create_secret(
        self, user_email: str, namespace: str, path: str, env: str, values: dict[str, str]
    ) -> Any:
        return self._unused("create_secret")

    def update_secret(
        self,
        user_email: str,
        namespace: str,
        path: str,
        env: str,
        values: dict[str, str],
        remove_keys: list[str],
    ) -> Any:
        return self._unused("update_secret")

    def delete_secret(self, user_email: str, namespace: str, path: str) -> Any:
        return self._unused("delete_secret")

    def reveal_value(self, user_email: str, namespace: str, path: str, env: str, key: str) -> Any:
        return self._unused("reveal_value")
