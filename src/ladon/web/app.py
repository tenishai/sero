"""The backend application (D14).

One process serves the API under ``/api``, an unauthenticated ``/healthz``, and
the built UI when a deployment points at one. ``create_app`` takes its
collaborators, so a test builds an instance over SQLite and a fake vault with no
patching; ``build_default_app`` is the zero-argument factory uvicorn calls.

Every failure leaves as ``{"error": "..."}`` with a status the UI can act on: 401
with no identity, 403 for a namespace or an admin route the caller may not have,
400 for a refused write, 404 for something absent, 503 for a deployment that has
not been configured yet. A message names the namespace, path, or field at fault
and never a value.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.exceptions import HTTPException

from ladon.apply.client import BaoConnection, BaoConnectionError, TokenAuth, connect
from ladon.datastore import engine as datastore
from ladon.hygiene import WriteRefused
from ladon.mcp.openbao import OpenBaoSecrets
from ladon.mcp.server import AccessDeniedError, WrongEnvironment
from ladon.web import routes_admin, routes_secrets
from ladon.web.assertion import AssertionVerifier
from ladon.web.deps import Services
from ladon.web.errors import ApiError
from ladon.web.groups import ConfigGroupSource, GroupSource
from ladon.web.identity import IdentityReader, resolve_dev_identity
from ladon.web.jobs import JobRunner
from ladon.web.reveal import AllowGate, DenyGate, RevealDeniedError, RevealGate
from ladon.web.service_lane import ServiceLane, build_service_lane
from ladon.web.service_lane import router as service_router
from ladon.web.settings import WebSettings, load_web_settings

API_PREFIX = "/api"

log = logging.getLogger(__name__)


def create_app(
    settings: WebSettings,
    *,
    session_factory: sessionmaker[Session] | None = None,
    secrets: Any | None = None,
    groups: GroupSource | None = None,
    reveal_gate: RevealGate | None = None,
    assertion_verifier: AssertionVerifier | None = None,
    service_lane: ServiceLane | None = None,
    env: Mapping[str, str] | None = None,
) -> FastAPI:
    """Build the application.

    Every collaborator has a default built from ``settings``; passing one
    replaces it, which is how the tests run against SQLite and a fake vault.
    """
    env = os.environ if env is None else env
    if not settings.environment:
        log.warning(
            "deployment.environment is not set: requests naming any environment "
            "are accepted. Declare the one environment this deployment serves."
        )
    groups = groups or ConfigGroupSource(settings.access)
    if secrets is None and settings.openbao_address:
        secrets = _live_secrets(settings, groups)

    if assertion_verifier is None and settings.identity.assertion is not None:
        assertion_verifier = AssertionVerifier(settings.identity.assertion)
    if assertion_verifier is None:
        log.warning(
            "identity.assertion is not configured: the identity header is an "
            "unverified claim, and this process must be reachable only through "
            "the authenticating proxy. See the trust boundary in docs/backend.md."
        )

    sessions = session_factory if session_factory is not None else datastore.session_factory()
    jobs = JobRunner(sessions) if sessions is not None else None
    if jobs is not None:
        # This process is the only thing that was running them, so whatever the
        # last one left behind ended when it did. Said here, at startup, before
        # a stale row can refuse the first job of its kind.
        jobs.recover_interrupted()

    app = FastAPI(title="Ladon", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = Services(
        settings=settings,
        identity=IdentityReader(
            header=settings.identity.header,
            dev_identity=resolve_dev_identity(settings.identity, env),
            assertion=assertion_verifier,
        ),
        groups=groups,
        reveal_gate=reveal_gate or (AllowGate() if settings.reveal_enabled else DenyGate()),
        session_factory=sessions,
        secrets=secrets,
        jobs=jobs,
    )
    # The machine lane's own state (D16): a verifier and a call budget per
    # configured caller. With none configured the lane refuses everything,
    # which is what an unconfigured deployment should answer a machine.
    app.state.service_lane = service_lane or build_service_lane(settings.service_callers)

    _register_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, bool]:
        """Liveness. Unauthenticated on purpose: a probe carries no identity."""
        return {"ok": True}

    app.include_router(routes_secrets.router, prefix=API_PREFIX)
    app.include_router(routes_admin.router, prefix=API_PREFIX)
    app.include_router(service_router, prefix=API_PREFIX)
    _register_ui(app, settings.ui_dist)
    return app


def build_default_app() -> FastAPI:
    """The factory uvicorn runs: ``uvicorn --factory ladon.web.app:build_default_app``."""
    return create_app(load_web_settings())


def _live_secrets(settings: WebSettings, groups: GroupSource) -> OpenBaoSecrets:
    """An ``OpenBaoSecrets`` over real, per-namespace OpenBao clients.

    One authenticated client per OpenBao namespace, built on first use and kept:
    a client carries a pooled TLS session, so rebuilding it per request would
    hand a handshake to every read. The token comes from the environment through
    ``TokenAuth.from_env``; it is never read from the config file.
    """
    clients: dict[str, Any] = {}
    lock = threading.Lock()

    def client_for(namespace: str) -> Any:
        with lock:
            if namespace not in clients:
                clients[namespace] = connect(
                    BaoConnection(address=str(settings.openbao_address), namespace=namespace),
                    TokenAuth.from_env(),
                )
            return clients[namespace]

    return OpenBaoSecrets(
        client_for=client_for,
        namespaces_for=groups.namespaces_for,
        mount=settings.kv_mount,
    )


def _register_error_handlers(app: FastAPI) -> None:
    """Map every failure the routes can raise onto the one error shape."""

    def error(status_code: int, message: str) -> JSONResponse:
        return JSONResponse(status_code=status_code, content={"error": message})

    @app.exception_handler(ApiError)
    def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"error": exc.message, **exc.details}
        )

    @app.exception_handler(AccessDeniedError)
    def _denied(request: Request, exc: AccessDeniedError) -> JSONResponse:
        return error(403, str(exc))

    @app.exception_handler(RevealDeniedError)
    def _reveal_denied(request: Request, exc: RevealDeniedError) -> JSONResponse:
        return error(403, str(exc))

    @app.exception_handler(WrongEnvironment)
    def _wrong_environment(request: Request, exc: WrongEnvironment) -> JSONResponse:
        return error(400, str(exc))

    @app.exception_handler(WriteRefused)
    def _refused(request: Request, exc: WriteRefused) -> JSONResponse:
        # A refusal is a decision the caller can act on: a placeholder value, a
        # key named twice, a merge that would empty a path, a reserved name.
        # Any other ValueError is a bug and falls to the handler below.
        return error(400, str(exc))

    @app.exception_handler(BaoConnectionError)
    def _vault_unreachable(request: Request, exc: BaoConnectionError) -> JSONResponse:
        # This module's errors never carry the token or a credentialed URL.
        return error(503, str(exc))

    @app.exception_handler(RequestValidationError)
    def _invalid_body(request: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        return error(422, f"invalid request: {where or 'body'}")

    @app.exception_handler(HTTPException)
    def _http(request: Request, exc: HTTPException) -> JSONResponse:
        return error(exc.status_code, str(exc.detail))

    @app.exception_handler(Exception)
    def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        # The traceback goes to the log and nothing else does: a path, a query
        # string or a body could name a secret, and an operator reading logs
        # does not need any of it to find the line that broke.
        log.exception("unhandled error while serving a request")
        return error(500, "internal error")


def _register_ui(app: FastAPI, ui_dist: Path | None) -> None:
    """Serve the built UI, falling back to the app shell for client-side routes.

    Registered last, so it only sees what no API route claimed. An unmatched
    ``/api`` path stays a JSON 404 rather than quietly returning the shell, which
    would turn a typo in the client into an unreadable parse error. A real route
    asked with the wrong method never reaches here at all: the router answers
    405, which the HTTPException handler renders in the same error shape.
    """
    root = ui_dist.resolve() if ui_dist is not None else None

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str) -> FileResponse:
        if full_path == "api" or full_path.startswith("api/") or root is None:
            raise ApiError(404, "not found")
        if full_path:
            candidate = (root / full_path).resolve()
            # Containment check: a path that climbs out of the bundle is not a
            # file this process will serve.
            if candidate.is_file() and candidate.is_relative_to(root):
                return FileResponse(candidate)
        index = root / "index.html"
        if not index.is_file():
            raise ApiError(404, "not found")
        return FileResponse(index)
