"""The machine lane: a service writing on a person's behalf (D16).

A machine is not a person with a proxy in front of it. A provisioner that
stands up an application needs to put that application's secret into OpenBao
and then make sure the Kubernetes Secret exists, and it does that on behalf of
whoever asked it to, hours after they asked. Letting it use the human lane
would mean either a long-lived human credential or a header nobody signed, so
it gets a lane of its own:

* an OIDC ID token, verified with exactly the machinery the human assertion
  uses (allow-listed algorithms, a key published at an https JWKS URL, issuer,
  audience, expiry, leeway and the outage grace);
* an audience that must differ from the human assertion's, refused at config
  load, so a person's browser credential is not a machine credential;
* a subject pinned per caller, so any other workload the same issuer trusts is
  still nobody here;
* the namespaces that caller may write, pinned in the same place, so reaching
  one namespace is not reaching every namespace;
* ``acting_email``, naming the person the write is for. It is recorded and
  never trusted: the caller's pinned namespaces authorize, and the address goes
  in the audit row so a machine write is still attributable to a human.

Nothing else is an identity here. The plain identity header is not read, the
development hatch is not read, and a deployment that has named no callers
refuses the lane outright: the lane opens because a deployment configured it,
never because it forgot to.

What a caller can do is deliberately small, and it is the life of one
application secret from end to end: merge keys into one path under ``apps/``,
list the key names at that path, delete the path outright, and converge or
remove one ExternalSecret for one application, in that application's own
Kubernetes namespace or one its config admits.

What is still somewhere else, behind a person: revealing a value, touching the
allow list, approving a namespace, running an apply phase, and off-boarding an
application (its store, its role, its policy and its service account). A
provisioner that can stand a secret up and take it down again is not a
provisioner that can dismantle an application's identity.
"""

from __future__ import annotations

import argparse
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Annotated, Any

import jwt
from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from ladon.apply.cli import PhaseRun, run_phase
from ladon.config.apps import (
    DEFAULT_APP_AUDIENCE,
    DEFAULT_SA_NAMESPACE,
    MAX_KV_PATH_LENGTH,
    app_store_name,
    checked_app_path,
    checked_apps_path,
    team_label_for_namespace,
)
from ladon.config.eso import (
    K8S_NAMESPACE_NAME_LABEL,
    cluster_store_ref,
    external_secret,
    external_secret_ref,
)
from ladon.config.teams import ConfigError
from ladon.hygiene import WriteRefused
from ladon.install.k8s import (
    ABSENT,
    DELETE,
    RECREATED,
    ApplyReport,
    ApplyResult,
    K8sError,
    converge_documents,
    delete_documents,
    eso_api_version,
    read_one,
    require_managed,
)
from ladon.mcp.server import (
    AccessDeniedError,
    _audit,
    create_secret_write,
    update_secret_write,
)
from ladon.web.assertion import (
    CLOCK_SKEW_LEEWAY_SECONDS,
    AssertionInvalid,
    AssertionVerifier,
    JwksFetcher,
    fetch_jwks,
)
from ladon.web.audit import DENIED, AuditRecorder
from ladon.web.deps import (
    UNIDENTIFIED,
    Auditor,
    DbSession,
    DeclaredEnvironment,
    Secrets,
    audit_recorder,
    services,
)
from ladon.web.errors import ApiError
from ladon.web.settings import ServiceCaller

# What the delete route answers when no delete ran and the object is still
# there: a dry run. Not one of the cluster layer's states, which describe a
# delete that happened.
UNCHANGED = "unchanged"

# The audit names for the things a machine may do. Prefixed, so one query
# separates what a machine did from what a person did. Each names its route, so
# an investigator reading a row does not have to know which verb it was.
PUT_TOOL = "service.put_secret"
SYNC_TOOL = "service.sync_secret"
READ_TOOL = "service.get_secret"
DELETE_TOOL = "service.delete_secret"
UNSYNC_TOOL = "service.delete_sync"

# Stamped on every row this lane writes. The tool name says what happened and
# this says who the "who" is: a row attributed to a person that a machine
# actually made. ``via`` names which machine.
LANE = "service"

# What a denial is attributed to when the token named no configured caller.
UNKNOWN_CALLER = "unknown"

AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "bearer "

# How old a machine token may be, whatever its own expiry says. An issuer that
# mints eight-hour tokens is not this lane's decision to live with: a token
# that has been sitting in a log, an environment variable or a queue is one
# somebody else may be holding by now.
MAX_SERVICE_TOKEN_AGE_SECONDS = 900

# How long this lane keeps using the last key set it fetched once refreshes
# start failing. Shorter than the human lane's hour: logging every person out
# of a UI during a key-endpoint outage is a real cost, and a machine retrying
# its provisioning call in ten minutes is not.
SERVICE_STALE_KEYS_MAX_SECONDS = 600

# A deliberately loose address check. It is not authorization and never decides
# anything; it catches a caller sending a username, an id or an empty string
# where the audit trail needs a person.
ACTING_EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"

# The longest address SMTP defines: 64 for the local part, 1 for the "@", 254
# for the whole. The pattern above says nothing about length, and this value
# reaches an audit row, so it is bounded like everything else that does.
ACTING_EMAIL_MAX = 254

# Kubernetes DNS-1123 label, the rule every name in a rendered document obeys.
LABEL_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
LABEL_MAX = 63

# An OpenBao namespace: one label, or several joined by "/", because namespaces
# nest and a team under a parent namespace is a path rather than a name. The
# same shape on both routes, so a nested deployment works on either.
NAMESPACE_PATTERN = r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(/[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$"
NAMESPACE_MAX = 253

# An ESO refresh interval: a whole number of seconds, minutes or hours. Bounded
# here because it is written verbatim into a manifest.
REFRESH_INTERVAL_PATTERN = r"^[1-9][0-9]*[smh]$"
DEFAULT_REFRESH_INTERVAL = "1h"

router = APIRouter()


class ServiceAuthError(Exception):
    """The token was absent, malformed, unverified, or not this caller's.

    Carries the caller name when the token at least named a configured one, so
    a denial can be audited against the caller it claimed to be.
    """

    def __init__(self, reason: str, *, caller: str | None = None) -> None:
        super().__init__(reason)
        self.caller = caller


class TokenBucket:
    """A per-caller call budget that refills over time.

    In-process and deliberately so: this is a guard against a stuck loop or a
    retry storm filling the vault, not a distributed quota. A second replica
    has a second bucket, which is the honest behaviour for a limit that costs
    no shared state.
    """

    def __init__(self, per_minute: int, *, now: Callable[[], float] = time.monotonic) -> None:
        self._capacity = float(per_minute)
        self._per_second = per_minute / 60.0
        self._tokens = float(per_minute)
        self._now = now
        self._stamp = now()
        self._lock = threading.Lock()

    def take(self) -> bool:
        """Spend one call, or answer False when the budget is gone."""
        with self._lock:
            moment = self._now()
            self._tokens = min(
                self._capacity, self._tokens + (moment - self._stamp) * self._per_second
            )
            self._stamp = moment
            if self._tokens < 1.0:
                return False
            self._tokens -= 1.0
            return True


@dataclass(frozen=True)
class ClusterSync:
    """How the lane reaches the cluster.

    Every collaborator is a field rather than a direct call, which is the same
    seam the CLI's tests use: a test hands over a fake cluster and nothing here
    shells out.
    """

    api_version: Callable[[], str] = eso_api_version
    converge: Callable[..., ApplyReport] = converge_documents
    remove: Callable[..., ApplyReport] = delete_documents
    read: Callable[..., dict[str, Any] | None] = read_one
    # The CLI's own app-onboarding phase, both halves, for an application whose
    # store is not there when a sync asks for it. The same function the admin
    # endpoint and the CLI run, so the lane never grows a second rendering of
    # an application's identity.
    onboard: Callable[..., PhaseRun] = run_phase


@dataclass(frozen=True)
class ServiceLane:
    """The configured machine callers, their verifiers, and their budgets."""

    verifiers: dict[tuple[str, str], tuple[ServiceCaller, AssertionVerifier]] = field(
        default_factory=dict
    )
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    # The read's own budget, per caller. Kept apart from the write budget so
    # that a caller polling to see whether an application has come up cannot
    # spend the allowance its own writes need: with one bucket, the call that
    # matters would be the one that gets refused.
    read_buckets: dict[str, TokenBucket] = field(default_factory=dict)
    cluster: ClusterSync = field(default_factory=ClusterSync)
    # Wall-clock, for comparing against the token's own ``iat``. Separate from
    # the buckets' monotonic clock, which measures elapsed time and nothing else.
    clock: Callable[[], float] = time.time

    def authenticate(self, authorization: str | None) -> ServiceCaller:
        """The caller this token proves, or :class:`ServiceAuthError`.

        The unverified claims choose which caller's settings to verify against
        and are trusted for nothing else: the verification that follows checks
        the signature, the issuer and the audience again, from configuration.
        """
        token = _bearer(authorization)
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception as exc:
            raise ServiceAuthError("the token is not a well-formed JWT") from exc

        match = self._match(unverified)
        if match is None:
            raise ServiceAuthError(
                "no configured service caller matches the token's issuer and audience"
            )
        caller, verifier = match
        try:
            claims = verifier.claims(token)
        except AssertionInvalid as exc:
            raise ServiceAuthError("the token did not verify", caller=caller.name) from exc
        if claims.get("sub") != caller.subject:
            raise ServiceAuthError(
                "the token's subject is not the one pinned to this caller",
                caller=caller.name,
            )
        self._require_fresh(claims, caller)
        return caller

    def allow(self, caller: ServiceCaller, *, read: bool = False) -> bool:
        """Spend one call from this caller's write budget, or its read budget."""
        buckets = self.read_buckets if read else self.buckets
        bucket = buckets.get(caller.name)
        return bucket.take() if bucket is not None else False

    def _require_fresh(self, claims: dict[str, Any], caller: ServiceCaller) -> None:
        """Refuse a token that is valid but old, or minted in the future.

        Expiry is the issuer's decision and issuers are generous with it. This
        lane writes secrets, so it sets its own ceiling on how long a minted
        token stays usable: past it, re-mint. A token stamped in the future is
        refused for the mirror reason, since it would otherwise be usable for
        its whole skew plus this window.
        """
        issued = claims.get("iat")
        if not isinstance(issued, int | float):
            raise ServiceAuthError("the token carries no usable 'iat'", caller=caller.name)
        age = self.clock() - float(issued)
        if age > MAX_SERVICE_TOKEN_AGE_SECONDS:
            raise ServiceAuthError(
                f"the token was issued more than {MAX_SERVICE_TOKEN_AGE_SECONDS} "
                "seconds ago; mint a fresh one",
                caller=caller.name,
            )
        if age < -CLOCK_SKEW_LEEWAY_SECONDS:
            raise ServiceAuthError("the token is stamped in the future", caller=caller.name)

    def _match(self, claims: dict[str, Any]) -> tuple[ServiceCaller, AssertionVerifier] | None:
        issuer = claims.get("iss")
        audience = claims.get("aud")
        # A token may carry several audiences. Config already refuses two
        # callers sharing an issuer and audience, so the only way to reach more
        # than one here is a token naming several: whose subject and namespaces
        # would apply is not a question worth guessing at, so it is refused.
        audiences = audience if isinstance(audience, list) else [audience]
        found = {
            (str(issuer), str(one))
            for one in audiences
            if (str(issuer), str(one)) in self.verifiers
        }
        if len(found) > 1:
            raise ServiceAuthError("the token names more than one configured service caller")
        return self.verifiers[found.pop()] if found else None


def build_service_lane(
    callers: Iterable[ServiceCaller],
    *,
    fetch: JwksFetcher = fetch_jwks,
    cluster: ClusterSync | None = None,
    now: Callable[[], float] = time.monotonic,
    clock: Callable[[], float] = time.time,
) -> ServiceLane:
    """One verifier and one budget per configured caller.

    A verifier per caller rather than one shared: each has its own key set, and
    caching them separately is what stops one caller's key rotation refetching
    everybody else's. Each keeps usable keys for a shorter grace than the human
    lane, because a retried provisioning call costs less than a logged-out UI.
    """
    callers = list(callers)
    return ServiceLane(
        verifiers={
            (caller.issuer, caller.audience): (
                caller,
                AssertionVerifier(
                    caller.assertion,
                    fetch=fetch,
                    stale_max_seconds=SERVICE_STALE_KEYS_MAX_SECONDS,
                ),
            )
            for caller in callers
        },
        buckets={
            caller.name: TokenBucket(caller.rate_limit_per_minute, now=now) for caller in callers
        },
        read_buckets={
            caller.name: TokenBucket(caller.read_limit_per_minute, now=now) for caller in callers
        },
        cluster=cluster or ClusterSync(),
        clock=clock,
    )


# ── Request bodies ─────────────────────────────────────────────────────


# The read takes its arguments in the query string rather than a body, so the
# same three fields are declared once here.
#
# Every argument on this lane carries a length, whether it travels in a query
# or a body, because each of them reaches an audit row: an unbounded one is a
# way to write as much as a caller cares to send into the trail with a single
# call. The bounds are the ones the values already have to obey elsewhere, so
# nothing that would have been accepted is refused here: a path may be as long
# as the store allows, a namespace as long as an OpenBao namespace, a label as
# long as a Kubernetes label, and an address as long as SMTP defines.
#
# ``acting_email`` is required on the read too: config refuses a caller that
# would not record who it acts for, and a read of which keys an application
# holds is as much that caller's business as a write is.
ActingEmailQuery = Annotated[str, Query(pattern=ACTING_EMAIL_PATTERN, max_length=ACTING_EMAIL_MAX)]
NamespaceQuery = Annotated[str, Query(pattern=NAMESPACE_PATTERN, max_length=NAMESPACE_MAX)]
PathQuery = Annotated[str, Query(max_length=MAX_KV_PATH_LENGTH)]


class ServicePutBody(BaseModel):
    """One application secret, merged."""

    # Required: a machine write with nobody behind it is not one this lane does.
    acting_email: str = Field(pattern=ACTING_EMAIL_PATTERN, max_length=ACTING_EMAIL_MAX)
    namespace: str = Field(pattern=NAMESPACE_PATTERN, max_length=NAMESPACE_MAX)
    path: str = Field(max_length=MAX_KV_PATH_LENGTH)
    values: dict[str, str] = Field(default_factory=dict)
    remove_keys: list[str] = Field(default_factory=list)
    # Dry-run by default, like every other write surface in Ladon.
    apply: bool = False


class ServiceDeleteBody(BaseModel):
    """One application path, removed outright."""

    acting_email: str = Field(pattern=ACTING_EMAIL_PATTERN, max_length=ACTING_EMAIL_MAX)
    namespace: str = Field(pattern=NAMESPACE_PATTERN, max_length=NAMESPACE_MAX)
    path: str = Field(max_length=MAX_KV_PATH_LENGTH)
    apply: bool = False


class ServiceSyncBody(BaseModel):
    """One ExternalSecret, converged against the application's own store."""

    acting_email: str = Field(pattern=ACTING_EMAIL_PATTERN, max_length=ACTING_EMAIL_MAX)
    namespace: str = Field(pattern=NAMESPACE_PATTERN, max_length=NAMESPACE_MAX)
    app: str = Field(pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    path: str = Field(max_length=MAX_KV_PATH_LENGTH)
    # Absent means the application's own name, which is where onboarding puts
    # it. Anywhere else has to be pinned to the caller in config.
    k8s_namespace: str | None = Field(default=None, pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    name: str = Field(pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    # The Secret name the consumer already uses, when it is not the same as the
    # ExternalSecret's own name.
    target_name: str | None = Field(default=None, pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    refresh_interval: str = Field(
        default=DEFAULT_REFRESH_INTERVAL, pattern=REFRESH_INTERVAL_PATTERN
    )
    apply: bool = False


class ServiceUnsyncBody(BaseModel):
    """One ExternalSecret, removed.

    No path, no store and no refresh interval: naming the object is all a
    removal needs, and asking for the rest would make a delete depend on
    values that may have moved since the object was created.
    """

    acting_email: str = Field(pattern=ACTING_EMAIL_PATTERN, max_length=ACTING_EMAIL_MAX)
    namespace: str = Field(pattern=NAMESPACE_PATTERN, max_length=NAMESPACE_MAX)
    app: str = Field(pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    k8s_namespace: str | None = Field(default=None, pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    name: str = Field(pattern=LABEL_PATTERN, max_length=LABEL_MAX)
    apply: bool = False


# ── Authentication as a dependency ─────────────────────────────────────


def lane(request: Request) -> ServiceLane:
    return request.app.state.service_lane


def _authenticated(request: Request, tool_name: str, *, read: bool = False) -> ServiceCaller:
    """The verified caller, or 401; then one call from its budget, or 429.

    ``read`` picks which budget. The read has its own, because a caller
    watching an application come up polls it, and one shared bucket would let
    that polling spend the allowance the same caller's writes need.

    Both refusals are audited before they are answered. A machine probing for a
    way in is exactly the pattern worth spotting, and without a row it is the
    one that leaves nothing behind. The row names the caller the token claimed,
    never the token.
    """
    recorder: AuditRecorder = audit_recorder(request)
    try:
        caller = lane(request).authenticate(request.headers.get(AUTHORIZATION_HEADER))
    except ServiceAuthError as exc:
        recorder.record(
            user_email=UNIDENTIFIED,
            tool_name=tool_name,
            status=DENIED,
            args={"lane": LANE, "via": exc.caller or UNKNOWN_CALLER},
            error=str(exc),
        )
        raise ApiError(401, "no service identity") from exc

    if not lane(request).allow(caller, read=read):
        recorder.record(
            # The body has not been read yet, so there is no person to name;
            # the caller is known, and filing it under the machine is what puts
            # the row beside that caller's other rows.
            user_email=caller.name,
            tool_name=tool_name,
            status=DENIED,
            args={"lane": LANE, "via": caller.name, "subject": caller.subject},
            error=(
                f"the caller has spent its {'read' if read else 'write'} budget for this minute"
            ),
        )
        raise ApiError(429, f"too many calls from {caller.name!r}; try again shortly")
    return caller


def put_caller(request: Request) -> ServiceCaller:
    return _authenticated(request, PUT_TOOL)


def sync_caller(request: Request) -> ServiceCaller:
    return _authenticated(request, SYNC_TOOL)


def read_caller(request: Request) -> ServiceCaller:
    return _authenticated(request, READ_TOOL, read=True)


def delete_caller(request: Request) -> ServiceCaller:
    return _authenticated(request, DELETE_TOOL)


def unsync_caller(request: Request) -> ServiceCaller:
    return _authenticated(request, UNSYNC_TOOL)


PutCaller = Annotated[ServiceCaller, Depends(put_caller)]
SyncCaller = Annotated[ServiceCaller, Depends(sync_caller)]
ReadCaller = Annotated[ServiceCaller, Depends(read_caller)]
DeleteCaller = Annotated[ServiceCaller, Depends(delete_caller)]
UnsyncCaller = Annotated[ServiceCaller, Depends(unsync_caller)]


# ── Routes ─────────────────────────────────────────────────────────────


@router.get("/service/secrets")
def get_secret(
    caller: ReadCaller,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
    environment: DeclaredEnvironment,
    acting_email: ActingEmailQuery,
    namespace: NamespaceQuery,
    path: PathQuery,
) -> dict[str, Any]:
    """Which keys one application path holds, by name.

    The one read on this lane, and it is narrower than the human one beside it:
    that returns a masked preview of every value, and this returns nothing but
    names. A mask is a hint about a value, and a caller assembling an
    application has no use for hints; it needs to know whether ``DB_URL`` is
    there. So the previews are read and dropped, and no value, masked or
    otherwise, can leave through here.

    The version and the timestamp come with them, which is what makes the
    answer useful for deciding whether a path needs writing: they say a path
    changed without saying what it changed to.

    A path that is not there is a ``404`` and still an audited read. The
    request was authorized and the lookup happened; what it found is in the
    row, because "did anything ask for this?" is a question worth answering
    about a path that does not exist.
    """
    args = {
        **_attribution(caller),
        "acting_email": acting_email,
        "namespace": namespace,
        "path": path,
    }
    with audit.guard(READ_TOOL, acting_email, args, session=session):
        _require_pinned(caller, namespace, action="read")
        checked = _refuse_outside_apps(path)
        args["path"] = checked
        detail = secrets.get_secret(acting_email, namespace, checked, environment or "")
        _audit(
            session,
            user_email=acting_email,
            tool_name=READ_TOOL,
            status="ok",
            args={**args, "exists": detail.exists},
        )
    session.commit()
    if not detail.exists:
        raise ApiError(404, f"there is no secret at {namespace}/{checked}")
    return {
        "namespace": namespace,
        "path": checked,
        # Names only, sorted, so two calls over the same path answer the same.
        "keys": sorted(detail.masked_keys),
        "version": detail.version,
        "updated_at": detail.updated_at,
    }


@router.post("/service/secrets")
def put_secret(
    body: ServicePutBody,
    caller: PutCaller,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
    environment: DeclaredEnvironment,
) -> dict[str, Any]:
    """Merge keys into one application path, on a person's behalf.

    The semantics are the ones the human surfaces have: values merge, named
    keys are removed, a key on both sides is refused, a merge that would empty
    the path is refused, placeholders are refused, and nothing is written
    without ``apply``. They are not reimplemented here: the same two functions
    the MCP tools and the UI's routes reach are what write.
    """
    args = {
        **_attribution(caller),
        "acting_email": body.acting_email,
        "namespace": body.namespace,
        "path": body.path,
    }
    extra = _attribution(caller)
    with audit.guard(PUT_TOOL, body.acting_email, args, session=session):
        _require_pinned(caller, body.namespace)
        path = _refuse_outside_apps(body.path)
        env = environment or ""
        current = secrets.get_secret(body.acting_email, body.namespace, path, env)
        if current.exists or body.remove_keys:
            result = update_secret_write(
                secrets,
                secrets,
                session,
                user_email=body.acting_email,
                namespace=body.namespace,
                path=path,
                env=env,
                values=body.values,
                remove_keys=body.remove_keys,
                apply=body.apply,
                deployment_environment=environment,
                tool_name=PUT_TOOL,
                audit_extra=extra,
            )
        else:
            result = create_secret_write(
                secrets,
                session,
                user_email=body.acting_email,
                namespace=body.namespace,
                path=path,
                env=env,
                values=body.values,
                apply=body.apply,
                deployment_environment=environment,
                tool_name=PUT_TOOL,
                audit_extra=extra,
            )
    session.commit()
    return result


@router.delete("/service/secrets")
def delete_secret(
    body: ServiceDeleteBody,
    caller: DeleteCaller,
    session: DbSession,
    secrets: Secrets,
    audit: Auditor,
    environment: DeclaredEnvironment,
) -> dict[str, Any]:
    """Remove one application path, and every version of it.

    The other half of provisioning. An application that is taken down leaves
    its secret behind otherwise, and a KV path nobody is watching is the one
    whose values go stale in place: still readable, still valid, still the
    credential somebody rotated everywhere except here.

    Metadata and all versions, so the path is gone rather than soft-deleted. A
    soft delete leaves something that still lists and can be undeleted, which
    is not what "remove it" means, and the versions are where the values are.

    Dry run by default, like every write on this lane. Without ``apply`` the
    answer says what it WOULD delete, by key name, and nothing is touched.
    With it, deleting a path that is already gone is ``deleted: false`` and a
    200, because a destroy step that fails when it is run twice is a destroy
    step nobody can retry.

    ``deleted`` comes from the read this route does first, and the delete runs
    whether or not that read found anything. The two are separate on purpose:
    the read is what the answer describes, and the delete is unconditional
    because OpenBao's metadata delete is already idempotent, so skipping it on
    the strength of a read taken a moment earlier only adds a race. A read that
    fails for any reason other than "not there" stops the call: a caller told
    ``deleted: false`` because Ladon could not look would believe a path was
    gone that is still live.
    """
    args = {
        **_attribution(caller),
        "acting_email": body.acting_email,
        "namespace": body.namespace,
        "path": body.path,
    }
    with audit.guard(DELETE_TOOL, body.acting_email, args, session=session):
        _require_pinned(caller, body.namespace, action="delete in")
        path = _refuse_outside_apps(body.path)
        args["path"] = path
        detail = secrets.get_secret(body.acting_email, body.namespace, path, environment or "")
        # Names only, read before the delete: afterwards there is nothing left
        # to ask, and a row that cannot say what was removed is half a record.
        keys = sorted(detail.masked_keys)
        if body.apply:
            secrets.delete_secret(body.acting_email, body.namespace, path)
        _audit(
            session,
            user_email=body.acting_email,
            tool_name=DELETE_TOOL,
            status="ok" if body.apply else "dry-run",
            args={**args, "key_names": keys, "existed": detail.exists},
        )
    session.commit()
    answer: dict[str, Any] = {
        "applied": body.apply,
        "namespace": body.namespace,
        "path": path,
        "keys": keys,
    }
    answer["deleted" if body.apply else "would_delete"] = detail.exists
    return answer


@router.post("/service/sync")
def sync_secret(
    body: ServiceSyncBody,
    request: Request,
    caller: SyncCaller,
    session: DbSession,
    audit: Auditor,
) -> dict[str, Any]:
    """Make sure the application's Kubernetes Secret exists.

    Writing the value into OpenBao is only half of it: until an ExternalSecret
    reads the path, the workload's Secret is not there and the pod does not
    start. One document, converged, so a second call over the same application
    reports it already current and writes nothing.

    The store is named by the same function that named it at onboarding, and the
    path must be inside that application's own prefix, so a document that points
    somewhere its identity cannot read is refused here rather than discovered in
    the operator's logs later.

    Where the object lands is an authorization decision of its own. The store's
    selector decides what ESO may read; it says nothing about where an object
    may be written, and this process holds the ``ladon`` field manager, so an
    unpinned Kubernetes namespace here would let one caller create or replace an
    ExternalSecret anywhere on the cluster. The default is the application's own
    namespace and anywhere else must be pinned to the caller in config.

    An object that is already there has to be this application's before it is
    converged, exactly as the ``DELETE`` requires before removing one. Every
    field that names it comes from the body, and a server-side apply under
    Ladon's own field manager takes the fields it sets from Ladon's own earlier
    apply without a word: it is the manager doing its job, as far as the API
    server can tell. So a caller pinned to one namespace could otherwise write
    over another team's live ExternalSecret, repoint it at a store it does
    control, and then pass the delete gate on the object it had just made its
    own. Checking here is what makes the delete gate mean anything.

    A ``409`` rather than a ``403`` when the object is real and this
    application's but another manager owns it; the apply would have taken its
    fields.
    """
    k8s_namespace = body.k8s_namespace or body.app
    target = body.target_name or body.name
    args = {
        **_attribution(caller),
        "acting_email": body.acting_email,
        "namespace": body.namespace,
        "app": body.app,
        "path": body.path,
        "k8s_namespace": k8s_namespace,
        "name": body.name,
        "target_name": target,
    }
    with audit.guard(SYNC_TOOL, body.acting_email, args, session=session):
        _require_pinned(caller, body.namespace)
        _require_cluster_target(caller, body.app, k8s_namespace, name=body.name, target=target)
        path = _refuse_outside_app(body.app, body.path)
        store = _store_for(body.namespace, body.app)
        args["store"] = store
        cluster = lane(request).cluster
        # Probed, never assumed: a resource rendered at a version the
        # cluster does not store is rejected on apply.
        api_version = _cluster_call(cluster.api_version)
        document = external_secret(
            name=body.name,
            namespace=k8s_namespace,
            store_name=store,
            kv_path=path,
            secret_name=target,
            refresh_interval=body.refresh_interval,
            api_version=api_version,
        )
        # An object that is not there yet is this caller's to create, and the
        # rules above are the whole check. One that is already there belongs to
        # somebody, and this is where it is established that the somebody is
        # this application. Settled BEFORE anything is written: a request that
        # ends in a refusal here must not have onboarded an identity on the
        # way, since nothing on this lane takes one back down.
        existing = _cluster_call(cluster.read, document, missing_ok=True)
        if existing is not None:
            _require_ours(
                existing,
                document,
                store=store,
                app=body.app,
                name=body.name,
                k8s_namespace=k8s_namespace,
            )
        # The identity the ExternalSecret reads through. An application the
        # room has just created has none yet, and an ExternalSecret pointing
        # at a store nothing created is a Secret that never arrives and a pod
        # that never starts. So the store's absence is the signal to onboard
        # the application here, from the same phase an operator would run,
        # before the document that depends on it is written. A store that is
        # there admits exactly the namespaces onboarding named, and a document
        # in any other namespace would be refused by ESO at refresh time, in
        # the operator's logs, later: the same dangling object one step on.
        onboarding: list[str] = []
        store_ref = cluster_store_ref(name=store, api_version=api_version)
        live_store = _cluster_call(cluster.read, store_ref, missing_ok=True)
        if live_store is None:
            onboarding = _onboard(
                request, body, k8s_namespace=k8s_namespace, onboard=cluster.onboard
            )
            # Recorded once it happened, and as what happened: a dry run
            # planned an identity, an apply created one. A refusal above
            # never reaches this line, so its row says neither.
            args["onboarded" if body.apply else "would_onboard"] = True
        else:
            _require_store_admits(live_store, store=store, k8s_namespace=k8s_namespace)
        report = _cluster_call(cluster.converge, [document], dry_run=not body.apply)
        _audit(
            session,
            user_email=body.acting_email,
            tool_name=SYNC_TOOL,
            status="ok" if body.apply else "dry-run",
            args=args,
        )
    session.commit()
    return {
        "applied": body.apply,
        "namespace": body.namespace,
        "app": body.app,
        "path": path,
        "store": store,
        "k8s_namespace": k8s_namespace,
        "name": body.name,
        "target_name": target,
        # Names and verbs only: what was converged, never what it carries.
        "report": report.lines(),
        "blocked": [result.target for result in report.blocked],
        # What onboarding the application did, in the same vocabulary; empty
        # when its identity was already there.
        "onboarding": onboarding,
    }


def _onboard(
    request: Request,
    body: ServiceSyncBody,
    *,
    k8s_namespace: str,
    onboard: Callable[..., PhaseRun],
) -> list[str]:
    """Onboard the application whose store the sync found missing.

    The four objects are the ones ``app-onboarding`` renders, from the same
    identity: the read-only policy on ``apps/<app>/`` in the team's OpenBao
    namespace, the JWT role on the cluster's mount bound to the application's
    own service account, that service account in Ladon's namespace, and the
    ClusterSecretStore admitted into the one Kubernetes namespace the caller
    named. Nothing here is rendered a second time: the phase runner is handed
    the flags an operator would type, with the OpenBao namespace the caller is
    pinned to (already checked) and the team label that namespace ends in.

    A dry-run sync plans the onboarding and writes nothing, an applied sync
    applies it. The OpenBao half converges first, and a blocked half, or one
    the cluster would not take, is a ``409`` before the ExternalSecret is
    touched: the whole point is to never leave a document pointing at an
    identity that did not land.

    The cluster the role hangs off is the deployment's, from config. A
    deployment that names none cannot onboard from this lane, and says so
    rather than guessing a mount.
    """
    settings = services(request).settings
    if not settings.cluster:
        raise ApiError(
            503,
            f"the application {body.app!r} has no ClusterSecretStore yet and this "
            "deployment names no cluster (deployment.cluster), so its identity cannot "
            "be onboarded from this lane; run app-onboarding for it first",
        )
    args = argparse.Namespace(
        phase="app-onboarding",
        config=None,
        kv_mount=None,
        namespace=body.namespace,
        team=team_label_for_namespace(body.namespace),
        cluster=settings.cluster,
        app=body.app,
        k8s_namespace=k8s_namespace,
        sa_namespace=DEFAULT_SA_NAMESPACE,
        audience=DEFAULT_APP_AUDIENCE,
        server=settings.openbao_address,
        teams=None,
        jwt_discovery_url=None,
        oidc_discovery_url=None,
        oidc_client_id=None,
        oidc_client_secret_ref=None,
    )
    try:
        run = onboard(
            args, config=settings.deployment, apply=body.apply, address=settings.openbao_address
        )
    except ConfigError as exc:
        # Every input here came from the deployment's own config (the team
        # label, the cluster, the address, the KV mount), never from the
        # caller, so a refusal from the phase runner is the operator's to fix.
        raise ApiError(503, f"onboarding {body.app!r} cannot run as deployed: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 - every failure here is reported, none carries a value
        # The phase runner raises on a vault it cannot reach, a token it cannot
        # use, or a cluster that will not answer. None of those is the caller's
        # mistake, and none of these messages names a secret value: onboarding
        # writes a policy, a role, a service account and a store, and nothing
        # in them is one.
        raise ApiError(503, f"onboarding {body.app!r} did not complete: {exc}") from exc
    lines = _onboarding_lines(run)
    if run.blocked or run.error:
        # The OpenBao half converges before the cluster half, so a blocked
        # run may well have landed a policy and a role already. The report
        # says what each operation did; this sentence does not guess.
        reason = run.error or "an operation in it is blocked; the report names which and why"
        raise ApiError(
            409,
            f"onboarding {body.app!r} is blocked, so its ExternalSecret was not written: {reason}",
            {"onboarding": lines},
        )
    return lines


def _require_store_admits(live: dict[str, Any], *, store: str, k8s_namespace: str) -> None:
    """Refuse a namespace the application's live store does not admit.

    Onboarding fixes a store to the namespaces it names (by the immutable
    ``kubernetes.io/metadata.name`` label). A store with no conditions admits
    every namespace and passes; one with conditions admits only those, and an
    ExternalSecret anywhere else is one ESO refuses at refresh time. Said here,
    as a ``409`` naming both, rather than discovered later in the operator's
    logs. Widening a store is off-boarding's sibling: an operator's decision
    about the application's identity, not a per-secret call on this lane.
    """
    conditions = (live.get("spec") or {}).get("conditions") or []
    admitted = []
    for condition in conditions:
        if not isinstance(condition, dict):
            continue
        labels = (condition.get("namespaceSelector") or {}).get("matchLabels") or {}
        if labels.get(K8S_NAMESPACE_NAME_LABEL):
            admitted.append(labels[K8S_NAMESPACE_NAME_LABEL])
    if conditions and k8s_namespace not in admitted:
        raise ApiError(
            409,
            f"the store {store!r} admits only the Kubernetes namespace(s) "
            f"{', '.join(repr(ns) for ns in admitted) or '(none by name)'}, not "
            f"{k8s_namespace!r}; an ExternalSecret there would never be read. Widening "
            "the store is an operator's app-onboarding decision, not this lane's",
        )


def _onboarding_lines(run: PhaseRun) -> list[str]:
    """The run, op by op: an action, a target and, for a blocked op, why.
    Never a payload."""
    lines = [
        f"openbao {diff.summary}: {diff.action}" + (f" ({diff.reason})" if diff.reason else "")
        for diff in (run.openbao.diffs if run.openbao is not None else ())
    ]
    if run.cluster is not None:
        lines.extend(run.cluster.lines())
    return lines


@router.delete("/service/sync")
def unsync_secret(
    body: ServiceUnsyncBody,
    request: Request,
    caller: UnsyncCaller,
    session: DbSession,
    audit: Auditor,
) -> dict[str, Any]:
    """Remove the ExternalSecret this lane created, and only that.

    Deleting the KV path leaves the object that reads it, and ESO keeps
    reconciling it: the operator logs an error every interval over a path that
    is gone on purpose, and the Kubernetes Secret it owns stays where it is.
    So the two halves come apart in the same order they went together.

    Scope, stated so nobody has to infer it. This removes one ExternalSecret.
    It does NOT remove the application's ClusterSecretStore, its JWT role, its
    read-only policy or its service account, and it does not touch the KV path.
    Those are application off-boarding: they are cluster-scoped or
    namespace-wide, one of them is shared with anything else the application
    runs, and removing them is a decision about an application's whole
    identity rather than about one of its secrets. That belongs to an operator
    and an apply phase, not to a per-secret call on the machine lane.

    This removes the target Secret with it. Ladon renders every ExternalSecret
    with ``creationPolicy: Owner``, so the Kubernetes Secret carries an owner
    reference to it and the garbage collector takes the Secret when the owner
    goes. That is the intended outcome of taking an application down, and it is
    also the reason the checks below are what they are: the blast radius of
    this route is a running workload's Secret, not one custom resource.

    Three things are checked before anything is deleted.

    * **The object is Ladon's.** A live field manager that is not ``ladon`` is
      a ``409`` naming the manager that does own it, the same rule the apply
      side follows in never passing ``--force-conflicts``.
    * **The object belongs to the application the caller named, in the
      namespace the caller is pinned to.** ``app`` is a field in the body and
      ``k8s_namespace`` defaults to it, so without this a caller pinned to one
      OpenBao namespace could name any application at all and reach an
      ExternalSecret that Ladon manages for somebody else, passing the
      ownership check on the way. The live object has to read through this
      application's own store, whose name is derived here exactly as the POST
      derives it, and to pull a path inside ``apps/<app>/``. Either mismatch is
      a ``403``.
    * **The caller may place objects there**, the same pinning as the POST: the
      application's own Kubernetes namespace, or one pinned to the caller.

    Dry run by default, and removing an object that is already gone is not an
    error.
    """
    k8s_namespace = body.k8s_namespace or body.app
    args = {
        **_attribution(caller),
        "acting_email": body.acting_email,
        "namespace": body.namespace,
        "app": body.app,
        "k8s_namespace": k8s_namespace,
        "name": body.name,
    }
    with audit.guard(UNSYNC_TOOL, body.acting_email, args, session=session):
        _require_pinned(caller, body.namespace, action="delete in")
        # The target rule has nothing to decide here: a removal names no
        # Secret to adopt, so the object's own name stands in for it.
        _require_cluster_target(caller, body.app, k8s_namespace, name=body.name, target=body.name)
        store = _store_for(body.namespace, body.app)
        args["store"] = store
        cluster = lane(request).cluster
        document = external_secret_ref(
            name=body.name,
            namespace=k8s_namespace,
            api_version=_cluster_call(cluster.api_version),
        )
        # Read before the removal to check whose object this is. The removal
        # reads again, at the moment it deletes, to check the field manager;
        # these are different questions and the second one is the one that has
        # to be fresh. Nothing a caller can do changes the answer in between:
        # editing the live object would take cluster write access, which is
        # not what this lane hands out.
        live = _cluster_call(cluster.read, document, missing_ok=True)
        cascades = False
        if live is None:
            # Nothing to remove, and nothing to establish about an object that
            # is not there. Calling the removal anyway would open a second
            # window: it reads again, and an object created in between is one
            # this route never checked and would delete.
            report = _absent(document)
        else:
            _require_ours(
                live,
                document,
                store=store,
                app=body.app,
                name=body.name,
                k8s_namespace=k8s_namespace,
            )
            cascades = _owns_its_target(live)
            report = _cluster_call(cluster.remove, [document], dry_run=not body.apply)
        result = report.results[0]
        outcome = result.action
        state = result.state or (ABSENT if outcome == ABSENT else UNCHANGED)
        _audit(
            session,
            user_email=body.acting_email,
            tool_name=UNSYNC_TOOL,
            status="ok" if body.apply else "dry-run",
            args={
                **args,
                "outcome": outcome,
                "state": state,
                "removes_target_secret": cascades,
            },
        )
    session.commit()
    answer: dict[str, Any] = {
        "applied": body.apply,
        "namespace": body.namespace,
        "app": body.app,
        "store": store,
        "k8s_namespace": k8s_namespace,
        "name": body.name,
        # The Kubernetes Secret goes with the ExternalSecret that owns it, and
        # a caller about to take a workload's Secret away should be told so by
        # the dry run rather than find out from the workload. It stays true
        # while the removal is terminating: the Secret is not gone yet, and it
        # is going.
        "removes_target_secret": cascades,
        # Where the cluster is now, not what was asked for. ``gone`` is the
        # ordinary outcome; ``terminating`` means a finalizer has not run, so
        # the object and what it owns are still there for the moment;
        # ``recreated`` means somebody made a new object under the same name
        # after this one was removed, which is the answer a caller has to act
        # on rather than treat as done. ``absent`` and ``unchanged`` are the
        # two where no delete ran.
        "state": state,
        # Spelled out beside the state because it is the one a caller must not
        # skip past: what is there now is not what this call removed.
        "recreated": state == RECREATED,
        "report": report.lines(),
    }
    # False when the object was already gone, which is a success either way.
    answer["deleted" if body.apply else "would_delete"] = outcome == DELETE
    return answer


# ── Internals ──────────────────────────────────────────────────────────


def _bearer(authorization: str | None) -> str:
    """The token out of an ``Authorization: Bearer`` header."""
    if not authorization or not authorization.lower().startswith(BEARER_PREFIX):
        raise ServiceAuthError("the machine lane needs an Authorization: Bearer token")
    token = authorization[len(BEARER_PREFIX) :].strip()
    if not token:
        raise ServiceAuthError("the Authorization header carries no token")
    return token


def _attribution(caller: ServiceCaller) -> dict[str, str]:
    """What every row this lane writes says about who made it.

    The row is filed under the person the call names, because that is who the
    write is for. These three say it was a machine that made it, which one, and
    the identity that machine proved, so an audit query can separate a
    machine-on-behalf row from a person's own without matching on tool names.
    """
    return {"lane": LANE, "via": caller.name, "subject": caller.subject}


def _require_pinned(caller: ServiceCaller, namespace: str, *, action: str = "write in") -> None:
    """Refuse a namespace this caller's config does not pin to it.

    The one authorization decision on this lane. It rests on the token's
    subject and the deployment's config, never on the acting email, which the
    caller supplies and could say anything.

    One list covers reading and writing, and deliberately: a caller pinned to a
    namespace so it can provision applications there is a caller that may see
    what it provisioned. ``action`` only changes the sentence, so a refused read
    does not report itself as a refused write.
    """
    if namespace not in caller.namespaces:
        raise AccessDeniedError(
            f"the service caller {caller.name!r} may not {action} namespace {namespace!r}"
        )


def _require_cluster_target(
    caller: ServiceCaller, app: str, k8s_namespace: str, *, name: str, target: str
) -> None:
    """Refuse a cluster object this caller may not place.

    Two decisions, both about writing rather than reading. The namespace: a
    caller may always use the application's own, which is where onboarding puts
    it, and anywhere else has to be listed against the caller. The target: an
    ExternalSecret whose ``target.name`` differs from its own name adopts a
    Secret that already has a name somebody chose, which is also how it would
    take one over, so it is allowed only in a namespace a deployment admitted
    on purpose rather than in the default one every application gets.
    """
    if k8s_namespace != app and k8s_namespace not in caller.k8s_namespaces:
        raise AccessDeniedError(
            f"the service caller {caller.name!r} may not place an object in "
            f"Kubernetes namespace {k8s_namespace!r}"
        )
    if target != name and k8s_namespace not in caller.k8s_namespaces:
        raise AccessDeniedError(
            f"the service caller {caller.name!r} may not name a target Secret "
            f"in Kubernetes namespace {k8s_namespace!r}"
        )


def _require_the_applications_own(
    live: dict[str, Any], *, store: str, app: str, name: str, k8s_namespace: str
) -> None:
    """Refuse an ExternalSecret that is not this application's, in this namespace.

    The ownership check says Ladon manages the object. It does not say which of
    Ladon's tenants it belongs to, and everything that names the object on
    either route is a field in the request body: without this, a caller pinned
    to one OpenBao namespace could name another team's application and have its
    live ExternalSecret rewritten or removed, taking the Secret it owns with
    it.

    Three properties of the live object answer that, and all three come from
    how onboarding builds one.

    It reads through a ``ClusterSecretStore``, which is the only kind Ladon
    renders. ``secretStoreRef.kind`` defaults to the namespaced ``SecretStore``
    when a document omits it, so a name that matches in a ref of the other kind
    is a different object entirely and must not read as a match.

    That store is the application's own, whose name is derived from the
    namespace this caller is pinned to, so a store that is not it means the
    object is somebody else's.

    And it pulls a path inside ``apps/<app>/``, the only prefix that
    application's identity can read; an object with no ``dataFrom`` extract at
    all is not one Ladon rendered.

    Every refusal names the store this caller's own application would use and
    the prefix it would read, both of which are derived from the request. None
    of them says a word about what the live object holds: its store name
    carries another tenant's team and application, and a caller that may not
    touch an object may not learn about it either.
    """
    spec = live.get("spec") or {}
    reference = spec.get("secretStoreRef") or {}
    if reference.get("kind") != CLUSTER_STORE_KIND or reference.get("name") != store:
        raise AccessDeniedError(
            f"the ExternalSecret {name!r} in Kubernetes namespace {k8s_namespace!r} does not "
            f"read through the {CLUSTER_STORE_KIND} {store!r}; it does not belong to "
            f"application {app!r} in this caller's namespace"
        )
    prefix = f"apps/{app}/"
    keys = [(entry.get("extract") or {}).get("key") for entry in spec.get("dataFrom") or []]
    if not keys or not all(isinstance(key, str) and key.startswith(prefix) for key in keys):
        raise AccessDeniedError(
            f"the ExternalSecret {name!r} in Kubernetes namespace {k8s_namespace!r} does not "
            f"read a path under {prefix!r}; it does not belong to application {app!r}"
        )


def _require_ours(
    live: dict[str, Any],
    document: dict[str, Any],
    *,
    store: str,
    app: str,
    name: str,
    k8s_namespace: str,
) -> None:
    """Both questions about a live object, in the order that leaks least.

    Whose object it is first, because that refusal describes only the request.
    Who manages it second, because that one names the managers on the object,
    which is something to tell a caller about its own application and not about
    a stranger's.
    """
    _require_the_applications_own(
        live, store=store, app=app, name=name, k8s_namespace=k8s_namespace
    )
    require_managed(document, live)


# The only store kind Ladon renders. ESO's own default for a ref that names no
# kind is the namespaced ``SecretStore``, so this is never assumed.
CLUSTER_STORE_KIND = "ClusterSecretStore"

# ESO's default when a document names none, and what Ladon always renders: the
# Secret carries an owner reference to the ExternalSecret, so removing the one
# removes the other.
OWNER_CREATION_POLICY = "Owner"


def _owns_its_target(live: dict[str, Any]) -> bool:
    """Whether removing this object removes the Kubernetes Secret with it."""
    target = ((live.get("spec") or {}).get("target")) or {}
    return str(target.get("creationPolicy", OWNER_CREATION_POLICY)) == OWNER_CREATION_POLICY


def _absent(document: dict[str, Any]) -> ApplyReport:
    """The report for an object the route found was not there.

    A removal that never ran, said in the vocabulary one that did would use,
    so the answer's shape does not depend on which path produced it.
    """
    metadata = document.get("metadata") or {}
    return ApplyReport(
        results=(
            ApplyResult(
                document.get("kind", ""),
                metadata.get("namespace"),
                metadata.get("name", ""),
                ABSENT,
            ),
        ),
        dry_run=True,
    )


def _store_for(namespace: str, app: str) -> str:
    """The application's ClusterSecretStore name.

    Named by the same function that named it at onboarding, from the team's own
    label. An OpenBao namespace may be a nested path, and by the baseline
    convention a team's namespace ends in that team's name, so the last segment
    is the label onboarding used. A name the parts cannot make is a refusal
    rather than a traceback.
    """
    try:
        return app_store_name(team=team_label_for_namespace(namespace), app=app)
    except ConfigError as exc:
        raise WriteRefused(str(exc)) from exc


def _refuse_outside_apps(path: str) -> str:
    try:
        return checked_apps_path(path)
    except ConfigError as exc:
        raise WriteRefused(str(exc)) from exc


def _refuse_outside_app(app: str, path: str) -> str:
    try:
        return checked_app_path(app, path)
    except ConfigError as exc:
        raise WriteRefused(str(exc)) from exc


def _cluster_call(call: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run one cluster operation, turning a kubectl failure into a 503.

    A cluster that cannot be reached, or one with no operator on it, is not the
    caller's mistake. These errors name kinds, objects and fields and never a
    value, so the message is safe to return as it stands.
    """
    try:
        return call(*args, **kwargs)
    except K8sError as exc:
        raise ApiError(503, str(exc)) from exc
