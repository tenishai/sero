"""The backend's error shape (D14).

Every failure the UI can see is one JSON object with an ``error`` string, so the
client has one thing to read whatever went wrong. Messages name the namespace,
path, or field at fault and never a value, matching the rule the rest of Ladon
follows for logs and CLI output.
"""

from __future__ import annotations

from typing import Any


class ApiError(Exception):
    """A failure with a status code and a message meant for the caller.

    ``details`` are extra fields sent alongside the message, for the failures
    where the client's next move needs one: the id of the job that is already
    running, say. They name objects, never values.
    """

    def __init__(
        self, status_code: int, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.details = details or {}
