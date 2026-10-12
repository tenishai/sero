"""The audit recorder, under the name the web layer already imports it by.

The recorder itself is in :mod:`sero.audit`, because the agent surface records
the same rows and must not have to import the web surface to do it. This module
stays so the routes keep one import, and so where the routes get their auditor
from does not change with where it lives.
"""

from __future__ import annotations

from sero.audit import (
    DENIED,
    ERROR,
    REFUSED,
    UNIDENTIFIED,
    AuditRecorder,
    status_for,
)

__all__ = [
    "DENIED",
    "ERROR",
    "REFUSED",
    "UNIDENTIFIED",
    "AuditRecorder",
    "status_for",
]
