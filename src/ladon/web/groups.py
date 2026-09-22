"""Which namespaces a caller may reach, and who administers (D15).

``GroupSource`` is the seam. The first implementation reads a mapping in the
deployment config, because a directory sync is the heaviest part of the platform
this generalizes and is not needed to serve a handful of applications. A
directory-backed source replaces it without touching a caller: the backend wires
``namespaces_for`` into the ``OpenBaoSecrets`` the tool functions already take, so
membership is enforced in one place for both the HTTP and the agent surface.

Being an admin is about the admin routes, not about namespaces: an admin reaches
the namespaces the config gives them, like anyone else.
"""

from __future__ import annotations

from typing import Protocol

from ladon.web.settings import AccessSettings


class GroupSource(Protocol):
    """Resolves a caller to their namespaces and their admin standing."""

    def namespaces_for(self, email: str) -> list[str]:
        """The namespaces this caller may access, sorted and deduplicated."""
        ...

    def is_admin(self, email: str) -> bool:
        """Whether this caller may use the admin routes."""
        ...


class ConfigGroupSource:
    """A ``GroupSource`` backed by the deployment config's ``access:`` block."""

    def __init__(self, access: AccessSettings) -> None:
        self._access = access

    def namespaces_for(self, email: str) -> list[str]:
        key = email.strip().lower()
        found: set[str] = set(self._access.members.get(key, ()))
        for group, emails in self._access.group_members.items():
            if key in emails:
                found.update(self._access.groups.get(group, ()))
        return sorted(found)

    def is_admin(self, email: str) -> bool:
        return email.strip().lower() in self._access.admins
