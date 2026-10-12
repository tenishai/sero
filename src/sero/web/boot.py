"""Bringing the backend up (D14).

Two things happen before the first request: the datastore is migrated to head,
and the server starts. Migrating at boot keeps the schema and the code that
expects it in one deployable unit, so there is no separate step to forget and no
window where a new image runs against an old schema. A failed upgrade is fatal:
the process exits non-zero rather than serving against a schema it cannot trust.

``SERO_DATABASE_URL`` is the only thing that decides where. It is the same
variable the engine and Alembic already read, so there is no second source of
truth. SQLite is the development target (``sqlite:////data/sero.db``);
PostgreSQL is the answer for anything shared.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from alembic import command
from alembic.config import Config

from sero.datastore.engine import DB_URL_ENV

# Where the revision scripts live. Overridable so an image can lay the repo out
# differently without patching this module.
MIGRATIONS_DIR_ENV = "SERO_MIGRATIONS_DIR"

HOST_ENV = "SERO_HOST"
PORT_ENV = "SERO_PORT"
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - a container port is published, not exposed
DEFAULT_PORT = 8000

log = logging.getLogger(__name__)


class MigrationError(RuntimeError):
    """The datastore could not be brought to head. The process must not serve."""


def migrations_dir() -> Path:
    """The directory holding the Alembic revision scripts."""
    override = os.environ.get(MIGRATIONS_DIR_ENV)
    if override:
        return Path(override)
    # src/sero/web/boot.py -> the repository root, which holds migrations/.
    return Path(__file__).resolve().parents[3] / "migrations"


def run_migrations(url: str | None = None) -> bool:
    """Upgrade the datastore to head. Returns False when none is configured.

    An unconfigured datastore is not an error: the backend still starts and
    answers ``/healthz`` and ``/api/me``, which is the honest state of an
    instance nobody has given a database yet. A configured datastore that
    cannot be upgraded is an error.
    """
    url = url or os.environ.get(DB_URL_ENV)
    if not url:
        log.warning(
            "%s is not set: skipping migrations. Routes that need the datastore "
            "will answer 503 until it is configured.",
            DB_URL_ENV,
        )
        return False

    scripts = migrations_dir()
    if not scripts.is_dir():
        raise MigrationError(f"no migration scripts at {scripts}")

    config = Config()
    config.set_main_option("script_location", str(scripts))
    # migrations/env.py prefers this over the environment, so an explicit url
    # wins and nothing has to be written back into os.environ. A literal % in
    # the url (a percent-encoded password) is doubled, because Alembic reads
    # options back through configparser interpolation.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    try:
        command.upgrade(config, "head")
    except Exception as exc:  # noqa: BLE001 - normalized; the url may hold a password
        raise MigrationError(f"datastore upgrade failed: {type(exc).__name__}") from exc
    return True


def main() -> int:
    """Migrate, then serve. The entrypoint an image runs."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    try:
        run_migrations()
    except MigrationError as exc:
        log.error("%s", exc)
        return 1

    import uvicorn

    uvicorn.run(
        "sero.web.app:build_default_app",
        factory=True,
        host=os.environ.get(HOST_ENV, DEFAULT_HOST),
        port=int(os.environ.get(PORT_ENV, DEFAULT_PORT)),
    )
    return 0
