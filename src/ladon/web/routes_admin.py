"""The admin routes (D14, D15).

Admins come from the deployment config, not from the allow list, so a new
deployment has someone who can put the first person on it. Every decision here
writes an audit row through the same path the tool functions use.

The apply phases are here too, as jobs: a phase takes longer than a request can
be held open, so the endpoint starts one and answers with its id, and the
caller polls it. The inputs are the flags the CLI takes, with one exception
that is the point of the whole surface: the deployment config is the one this
process was started with, and a request cannot name another. Approving a
namespace request is the same machinery, narrowed to that one team.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from ladon.apply.cli import PhaseRun, build_plan, run_eso_install, run_phase
from ladon.config.apps import DEFAULT_APP_AUDIENCE, DEFAULT_SA_NAMESPACE
from ladon.config.teams import ConfigError
from ladon.datastore import repositories as repo
from ladon.datastore.models import Job, NamespaceRequest
from ladon.install.eso import DEFAULT_NAMESPACE as ESO_NAMESPACE
from ladon.install.k8s import ALREADY_CURRENT
from ladon.mcp.server import _audit
from ladon.web.deps import AdminEmail, Auditor, DbSession, Jobs, services
from ladon.web.errors import ApiError
from ladon.web.identity import normalize_email
from ladon.web.jobs import JobConflict, JobOutcome
from ladon.web.serializers import allow_list_entry, job_row, namespace_request_row
from ladon.web.settings import WebSettings

router = APIRouter(prefix="/admin")

# The phases that can be started as a job. Named exactly as the CLI names them,
# so an operator reading a job row and an operator reading a terminal are
# reading the same word.
ROOT_CONFIG = "root-config"
TEAM_ONBOARDING = "team-onboarding"
APP_ONBOARDING = "app-onboarding"
ESO_INSTALL = "eso-install"
APPLY_PHASES = (ROOT_CONFIG, TEAM_ONBOARDING, APP_ONBOARDING, ESO_INSTALL)

# How many jobs a listing returns when the caller does not say.
DEFAULT_JOB_LIMIT = 50
MAX_JOB_LIMIT = 200


class RejectBody(BaseModel):
    reason: str


class AllowListBody(BaseModel):
    email: str
    notes: str | None = None


class ApplyBody(BaseModel):
    """The inputs one apply phase takes: the CLI's flags for it.

    There is deliberately no ``config``: the deployment config is the one this
    process was started with, and a request that could name another could plan
    against teams, namespaces and mounts nobody deployed. An unknown field is
    refused rather than ignored, so a misspelled input is a 422 and not a job
    that quietly did something else.
    """

    model_config = ConfigDict(extra="forbid")

    # Dry-run by default, like every other write surface in Ladon.
    apply: bool = False
    # root-config
    teams: list[str] | None = None
    jwt_discovery_url: str | None = None
    oidc_discovery_url: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret_ref: str | None = None
    # team-onboarding and app-onboarding
    team: str | None = None
    cluster: str | None = None
    # app-onboarding
    app: str | None = None
    k8s_namespace: str | None = None
    sa_namespace: str = DEFAULT_SA_NAMESPACE
    audience: str = DEFAULT_APP_AUDIENCE
    server: str | None = None
    # The OpenBao namespace a phase's operations run in, overriding the one
    # resolved from the team; for eso-install, the Kubernetes namespace to
    # install into. Both are what --namespace means on the matching command.
    namespace: str | None = None


@router.get("/namespace-requests")
def list_namespace_requests(
    email: AdminEmail,
    session: DbSession,
    status: str | None = None,
) -> dict[str, Any]:
    rows = repo.list_namespace_requests(session, status=status)
    return {"requests": [namespace_request_row(r) for r in rows]}


@router.post("/namespace-requests/{request_id}/approve")
def approve_namespace_request(
    request_id: int,
    request: Request,
    email: AdminEmail,
    session: DbSession,
    jobs: Jobs,
) -> dict[str, Any]:
    """Approve a request and apply that one team's namespace.

    The run is a root-config apply narrowed to this team: its namespace, the
    deployment's KV mount inside it, and the policies its access level grants.
    Nothing at the root is touched, so approving one team's request cannot
    rewrite the deployment's auth methods.

    Approval and provisioning stay separate states: the decision is recorded
    here and stands, and the run that carries it out reports on the row. A team
    the deployment config does not define is refused before either happens,
    because the namespace, the mount and the policies all come from that entry.
    """
    settings = services(request).settings
    row = _decide(session, request_id, lambda: repo.get_namespace_request(session, request_id))
    body = ApplyBody(teams=[row.team_name], apply=True)
    args = _phase_args(ROOT_CONFIG, body, settings)
    try:
        build_plan(args, settings.deployment)
    except ConfigError as exc:
        raise ApiError(400, str(exc)) from exc

    # The job is claimed before the decision is written, so this request holds
    # no write open while the runner takes one of its own: they are different
    # connections to the same datastore, and each waits for the other's lock.
    job_id = _submit(
        jobs,
        phase=ROOT_CONFIG,
        email=email,
        work=_work(ROOT_CONFIG, args, body, settings),
        on_finish=_record_on_request(request_id),
    )
    row = repo.mark_request_approved(session, request_id=request_id)
    repo.record_request_job(session, request_id=request_id, job_id=job_id)
    _audit(
        session,
        user_email=email,
        tool_name="approve_namespace_request",
        status="ok",
        args={"request_id": request_id, "team_name": row.team_name, "job_id": job_id},
    )
    session.commit()
    return namespace_request_row(row)


def _record_on_request(request_id: int) -> Callable[[Session, Job], None]:
    """Put the run's outcome on the request it was approving.

    The admin looks at the request, not at a job listing, so that is where a
    run that did not land has to say so, and where the last run's error stops
    being shown once one lands. The decision is unchanged either way: it was
    approved, and it still is.
    """

    def record(session: Session, job: Job) -> None:
        repo.record_request_error(session, request_id=request_id, error=job.error)

    return record


@router.post("/namespace-requests/{request_id}/reject")
def reject_namespace_request(
    request_id: int,
    body: RejectBody,
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    row = _decide(
        session,
        request_id,
        lambda: repo.mark_request_rejected(session, request_id=request_id, reason=body.reason),
    )
    _audit(
        session,
        user_email=email,
        tool_name="reject_namespace_request",
        status="ok",
        args={"request_id": request_id, "reason": body.reason},
    )
    session.commit()
    return namespace_request_row(row)


@router.get("/allow-list")
def list_allow_list(
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    return {"entries": [allow_list_entry(e) for e in repo.list_allowed(session)]}


@router.post("/allow-list")
def add_to_allow_list(
    body: AllowListBody,
    email: AdminEmail,
    session: DbSession,
) -> dict[str, Any]:
    added = normalize_email(body.email)
    if not added:
        raise ApiError(400, "an allow-list entry needs an email")
    entry = repo.allow(session, email=added, added_by=email, notes=body.notes)
    _audit(
        session,
        user_email=email,
        tool_name="allow_list_add",
        status="ok",
        args={"email": added},
    )
    session.commit()
    return allow_list_entry(entry)


@router.delete("/allow-list/{entry_email}", status_code=204)
def remove_from_allow_list(
    entry_email: str,
    email: AdminEmail,
    session: DbSession,
) -> Response:
    removed = normalize_email(entry_email)
    if not repo.deny(session, email=removed):
        raise ApiError(404, f"{removed} is not on the allow list")
    _audit(
        session,
        user_email=email,
        tool_name="allow_list_remove",
        status="ok",
        args={"email": removed},
    )
    session.commit()
    return Response(status_code=204)


def _decide(
    session: Session, request_id: int, decide: Callable[[], NamespaceRequest]
) -> NamespaceRequest:
    """Run a decision, turning an unknown request into a 404 rather than a 500."""
    try:
        return decide()
    except KeyError as exc:
        raise ApiError(404, f"namespace request {request_id} not found") from exc


# ── The apply phases, as jobs ──────────────────────────────────────────


@router.post("/apply/{phase}", status_code=202)
def start_apply(
    phase: str,
    body: ApplyBody,
    request: Request,
    email: AdminEmail,
    jobs: Jobs,
    audit: Auditor,
) -> dict[str, Any]:
    """Start one apply phase in the background. Dry-run unless ``apply``."""
    if phase not in APPLY_PHASES:
        raise ApiError(404, f"no apply phase named {phase!r}")
    settings = services(request).settings
    args = _phase_args(phase, body, settings)
    if phase != ESO_INSTALL:
        # Planning is offline and pure, so a body that cannot produce a plan is
        # refused here, while the caller is still listening, rather than
        # becoming a job that fails a second later for a reason nobody reads.
        try:
            build_plan(args, settings.deployment)
        except ConfigError as exc:
            raise ApiError(400, str(exc)) from exc
    job_id = _submit(jobs, phase=phase, email=email, work=_work(phase, args, body, settings))
    audit.record(
        user_email=email,
        tool_name=f"apply_{phase}",
        status="ok",
        args={"kind": phase, "job_id": job_id, "apply": body.apply, "args": _audited(body)},
    )
    return {"job_id": job_id, "kind": phase, "state": "queued"}


@router.get("/jobs/{job_id}")
def read_job(job_id: int, email: AdminEmail, session: DbSession) -> dict[str, Any]:
    """One job: its state, how long it has taken, and its op-by-op report."""
    row = repo.get_job(session, job_id)
    if row is None:
        raise ApiError(404, f"job {job_id} not found")
    return job_row(row)


@router.get("/jobs")
def list_jobs(
    email: AdminEmail,
    session: DbSession,
    kind: str | None = None,
    limit: int = DEFAULT_JOB_LIMIT,
) -> dict[str, Any]:
    """Recent jobs, newest first."""
    rows = repo.list_jobs(session, kind=kind, limit=max(1, min(limit, MAX_JOB_LIMIT)))
    return {"jobs": [job_row(row) for row in rows]}


def _submit(jobs: Jobs, *, phase: str, email: str, work, on_finish=None) -> int:
    """Claim the phase, or answer with the job that already holds it."""
    try:
        return jobs.submit(kind=phase, requested_by=email, work=work, on_finish=on_finish)
    except JobConflict as exc:
        raise ApiError(409, str(exc), {"job_id": exc.job_id, "kind": exc.kind}) from exc


def _phase_args(phase: str, body: ApplyBody, settings: WebSettings) -> argparse.Namespace:
    """The flags this phase runs with, as the apply layer reads them.

    ``config`` is None because the caller passes the deployment's own loaded
    config instead, and the KV mount comes from it. The address the rendered
    store dials defaults to the one this deployment talks to, so the store and
    the writes agree about which OpenBao this is.
    """
    return argparse.Namespace(
        phase=phase,
        config=None,
        kv_mount=None,
        namespace=body.namespace,
        teams=body.teams,
        team=body.team,
        cluster=body.cluster,
        app=body.app,
        k8s_namespace=body.k8s_namespace,
        sa_namespace=body.sa_namespace,
        audience=body.audience,
        server=body.server or settings.openbao_address,
        jwt_discovery_url=body.jwt_discovery_url,
        oidc_discovery_url=body.oidc_discovery_url,
        oidc_client_id=body.oidc_client_id,
        oidc_client_secret_ref=body.oidc_client_secret_ref,
    )


def _work(phase: str, args: argparse.Namespace, body: ApplyBody, settings: WebSettings):
    """What the job's thread runs. Closes over inputs, never over the request."""
    if phase == ESO_INSTALL:
        namespace = body.namespace or ESO_NAMESPACE

        def install() -> JobOutcome:
            result = run_eso_install(namespace, apply=body.apply)
            return JobOutcome(
                report=(
                    {
                        "surface": "cluster",
                        "action": ALREADY_CURRENT if result.reused else "install",
                        "target": "external-secrets operator",
                        "reason": result.summary,
                    },
                )
            )

        return install

    def apply_phase() -> JobOutcome:
        run = run_phase(
            args,
            config=settings.deployment,
            apply=body.apply,
            address=settings.openbao_address,
        )
        return JobOutcome(report=_report(run), error=_failure(run))

    return apply_phase


def _report(run: PhaseRun) -> tuple[dict[str, Any], ...]:
    """The run, op by op: what each operation did and to which object.

    Every line is an action and a target, plus which fields differ and why an
    operation is blocked. A payload never appears: not a value, not a policy
    body, not a token.
    """
    entries: list[dict[str, Any]] = []
    for diff in run.openbao.diffs if run.openbao is not None else ():
        entry: dict[str, Any] = {
            "surface": "openbao",
            "action": diff.action,
            "target": diff.summary,
        }
        if diff.changes:
            entry["changes"] = [change.render() for change in diff.changes]
        if diff.reason:
            entry["reason"] = diff.reason
        entries.append(entry)
    for result in run.cluster.results if run.cluster is not None else ():
        entry = {"surface": "cluster", "action": result.action, "target": result.target}
        if result.reason:
            entry["reason"] = result.reason
        entries.append(entry)
    return tuple(entries)


def _failure(run: PhaseRun) -> str | None:
    """Why this run did not succeed, or None.

    A blocked operation is not an exception: nothing was written, and the
    report names the object and the reason. It is not a success either, so the
    job says so rather than reporting done and leaving the reading to somebody.
    """
    if run.error:
        return run.error
    blocked = len(run.openbao.blocked) if run.openbao is not None else 0
    blocked += len(run.cluster.blocked) if run.cluster is not None else 0
    if blocked:
        return (
            f"{blocked} operation(s) blocked, so nothing was written. "
            "The report names each one and why."
        )
    return None


def _audited(body: ApplyBody) -> dict[str, Any]:
    """The inputs, for the audit row: the ones that were given, by name."""
    return {key: value for key, value in body.model_dump(exclude={"apply"}).items() if value}
