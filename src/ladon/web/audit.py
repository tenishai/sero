"""Audit rows that survive the operation they describe (D14).

The tool functions write their audit row into the caller's session, which is the
right thing when the call succeeds: the row and the write land together. It is
the wrong thing when the call fails, because the exception unwinds the session
and takes the row with it, so exactly the calls worth investigating leave no
trace.

So a failure is audited in its own short transaction: open, write, commit,
close. It is independent of the request's session, which is free to roll back.

An audit write that itself fails is logged and swallowed. This helper only runs
while an operation is already failing, and raising here would replace the real
error with a less useful one.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from ladon.datastore import repositories as repo
from ladon.hygiene import WriteRefused
from ladon.mcp.server import AccessDeniedError, WrongEnvironment
from ladon.web.reveal import RevealDeniedError

log = logging.getLogger(__name__)

# Three kinds of unhappy ending, so an investigator can tell them apart at a
# glance: we would not let you (denied), you asked for something we do not do
# (refused), or we broke (error).
DENIED = "denied"
REFUSED = "refused"
ERROR = "error"


def status_for(exc: BaseException) -> str:
    """Which kind of unhappy ending an exception represents."""
    if isinstance(exc, AccessDeniedError | RevealDeniedError):
        return DENIED
    if isinstance(exc, WriteRefused | WrongEnvironment):
        return REFUSED
    return ERROR


@dataclass(frozen=True)
class AuditRecorder:
    """Writes audit rows outside the request's transaction."""

    session_factory: sessionmaker[Session] | None

    def record(
        self,
        *,
        user_email: str,
        tool_name: str,
        status: str,
        args: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        """Write one row and commit it. Never raises."""
        if self.session_factory is None:
            return
        try:
            with self.session_factory() as session:
                repo.append_call(
                    session,
                    user_email=user_email,
                    method="tools/call",
                    tool_name=tool_name,
                    status=status,
                    duration_ms=0,
                    args=args,
                    error=error,
                )
                session.commit()
        except Exception:
            # Already on a failure path; losing the row is better than replacing
            # the caller's error with this one.
            log.exception("could not write an audit row for %s", tool_name)

    @contextmanager
    def guard(
        self,
        tool_name: str,
        user_email: str,
        args: dict[str, Any] | None = None,
        *,
        session: Session | None = None,
    ) -> Iterator[None]:
        """Audit whatever goes wrong inside, then let it propagate.

        ``args`` names the operation (namespace, path, environment) and never
        carries values: the successful path's row, written by the tool function,
        is the one that records the full redacted arguments.

        The request's ``session`` is rolled back first. Its own audit row is
        already doomed, and on SQLite it holds the write lock that this row needs.
        """
        try:
            yield
        except Exception as exc:
            if session is not None:
                session.rollback()
            self.record(
                user_email=user_email,
                tool_name=tool_name,
                status=status_for(exc),
                args=args,
                error=str(exc),
            )
            raise
