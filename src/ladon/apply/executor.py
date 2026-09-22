"""Execute an apply plan against a live OpenBao (D3b, the thin SDK wrapper).

The planning layer (``ladon.apply.plan``) is pure: it produces an ordered,
value-free :class:`~ladon.apply.plan.Plan`. This module is the only part that
talks to OpenBao. It mirrors ``ladon.install``: execution defaults to a dry-run,
and an apply must be requested explicitly with ``apply=True``, per the
secrets-hygiene rule that writes are dry-run by default.

Every op is a *desired state*, not a blind write. Before writing, the executor
reads the live object and classifies the op:

* ``create`` when the object is absent;
* ``update`` when it exists and differs, naming the fields that differ;
* ``already current`` when it matches, in which case nothing is written.

That is what makes a repeated apply converge instead of failing on the first
mount that already exists, and it is what lets a dry-run show a real diff
against live state rather than a list of intentions. After each write the object
is read back and compared again: a policy that silently fails to land is worse
than one that fails loudly.

Each :class:`~ladon.apply.plan.WriteOp` kind maps to one handler holding
everything kind-specific: how to read the live state, what the desired state is,
how to create it, and how to update it. The two enable/mount operations use
hvac's typed system-backend helpers; the path writes (auth config, auth roles,
and anything later phases add) use the generic ``write_data`` so the plan layer
stays the single source of truth for what a path write contains.

A dry-run with no client touches nothing and still returns the plan's value-free
render. This module never logs a payload: op summaries are kind and path only,
and a field whose name or shape makes it unsafe to show is reported as differing
rather than printed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ladon.apply.plan import Plan, WriteOp

# This module does not import hvac: the caller constructs and authenticates the
# ``hvac.Client`` and passes it in. That keeps the executor testable with a fake
# client and free of any connection concern, and leaves the hvac pin (pyproject,
# docs/versions.md) to the layer that builds the client.


class ApplyError(RuntimeError):
    """An op could not be read, applied, or verified, or the client is unusable."""


# The three outcomes of diffing one op against live state.
CREATE = "create"
UPDATE = "update"
CURRENT = "already current"

# Sentinel for a field the live object does not carry at all, distinct from a
# field that is present and empty.
_ABSENT = object()


@dataclass(frozen=True)
class FieldChange:
    """One field that differs between the desired and the live object.

    ``old`` and ``new`` are short, single-line renderings, or None when the value
    must not be shown (a secret-ish field name, a multi-line body such as policy
    HCL, or an overlong value). A change with nothing shown still names its
    field, which is what a reviewer needs.
    """

    field: str
    old: str | None = None
    new: str | None = None

    def render(self) -> str:
        if self.old is None or self.new is None:
            return f"{self.field}: differs"
        return f"{self.field}: {self.old} -> {self.new}"


@dataclass(frozen=True)
class OpDiff:
    """What one op would do (or did) against live state."""

    summary: str
    action: str
    changes: tuple[FieldChange, ...] = ()

    @property
    def needs_write(self) -> bool:
        return self.action != CURRENT

    def render(self) -> str:
        if not self.changes:
            return f"{self.action} {self.summary}"
        detail = ", ".join(c.render() for c in self.changes)
        return f"{self.action} {self.summary} ({detail})"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of executing (or dry-running) a plan.

    ``diffs`` carries one :class:`OpDiff` per op whenever a client was available
    to read live state. ``applied`` is the summary line of each op that was
    actually written, so an op that was already current does not appear there; on
    a dry-run it is empty and ``planned`` carries every op summary instead.
    Neither list ever contains a payload.
    """

    applied: tuple[str, ...] = ()
    planned: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = True
    diffs: tuple[OpDiff, ...] = ()


# --- reading live state ------------------------------------------------------


def _mount_name(op: WriteOp) -> str:
    # "sys/mounts/<mount>" and "sys/auth/<mount>" both name the mount last.
    return op.path.rsplit("/", 1)[-1]


def _is_not_found(exc: Exception) -> bool:
    # hvac raises InvalidPath for a 404; matched by name so this module keeps its
    # no-hvac-import rule. Anything else is a real failure and must not be read
    # as "the object does not exist", which would turn a denied read into a write.
    return type(exc).__name__ == "InvalidPath" or getattr(exc, "status_code", None) == 404


def _call(fn: Callable, *args, **kwargs):
    """Call a read, returning None when the object does not exist."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001 - only a 404 is swallowed; the rest re-raise
        if _is_not_found(exc):
            return None
        raise


def _unwrap(response) -> dict:
    """The payload of an OpenBao response: under ``data``, or the body itself."""
    if not isinstance(response, dict):
        return {}
    data = response.get("data")
    return data if isinstance(data, dict) else response


def _read_secrets_engine(client, op: WriteOp) -> dict | None:
    mounts = _unwrap(_call(client.sys.list_mounted_secrets_engines))
    mount = mounts.get(f"{_mount_name(op)}/")
    if not isinstance(mount, dict):
        return None
    options = mount.get("options") or {}
    return {"type": mount.get("type"), "version": options.get("version")}


def _desired_secrets_engine(op: WriteOp) -> dict:
    options = op.data.get("options") or {}
    return {"type": op.data["type"], "version": options.get("version")}


def _read_auth_method(client, op: WriteOp) -> dict | None:
    methods = _unwrap(_call(client.sys.list_auth_methods))
    method = methods.get(f"{_mount_name(op)}/")
    if not isinstance(method, dict):
        return None
    return {"type": method.get("type")}


def _desired_auth_method(op: WriteOp) -> dict:
    return {"type": op.data["type"]}


def _read_policy(client, op: WriteOp) -> dict | None:
    name = op.path.rsplit("/", 1)[-1]
    body = _call(client.sys.read_policy, name=name)
    if body is None:
        return None
    live = _unwrap(body)
    # The read endpoint returns the body under "rules"; newer responses use
    # "policy". Either way it is the same HCL.
    hcl = live.get("rules") or live.get("policy")
    if hcl is None:
        return None
    return {"policy": _normalise_hcl(hcl)}


def _desired_policy(op: WriteOp) -> dict:
    return {"policy": _normalise_hcl(op.data["policy"])}


def _read_path(client, op: WriteOp) -> dict | None:
    response = _call(client.read, op.path)
    if response is None:
        return None
    return _unwrap(response)


def _desired_path(op: WriteOp) -> dict:
    return dict(op.data)


# --- writing -----------------------------------------------------------------


def _create_secrets_engine(client, op: WriteOp) -> None:
    client.sys.enable_secrets_engine(
        backend_type=op.data["type"],
        path=_mount_name(op),
        options=op.data.get("options"),
    )


def _tune_secrets_engine(client, op: WriteOp) -> None:
    # Only the mount options can change in place; the engine type cannot (see
    # _IMMUTABLE below).
    client.sys.tune_mount_configuration(path=_mount_name(op), options=op.data.get("options"))


def _create_auth_method(client, op: WriteOp) -> None:
    client.sys.enable_auth_method(method_type=op.data["type"], path=_mount_name(op))


def _write_policy(client, op: WriteOp) -> None:
    name = op.path.rsplit("/", 1)[-1]
    client.sys.create_or_update_policy(name=name, policy=op.data["policy"])


def _path_write(client, op: WriteOp) -> None:
    # auth config and roles are plain path writes; the op path is already the API
    # path hvac expects.
    client.write_data(op.path, data=op.data)


@dataclass(frozen=True)
class _Handler:
    """Everything kind-specific: read live state, desired state, create, update.

    ``update`` is None for an object that exists in one shape only, so a
    difference in it can only be a conflict.
    """

    read: Callable[[object, WriteOp], dict | None]
    desired: Callable[[WriteOp], dict]
    create: Callable[[object, WriteOp], None]
    update: Callable[[object, WriteOp], None] | None = None


# Fields that cannot be changed on a live object. Converging them would mean
# destroying and recreating the object, taking its data with it, so Ladon
# refuses and says so instead.
_IMMUTABLE = frozenset({"type"})

# Dispatch from a WriteOp.kind to its handler. A kind absent from this table is a
# programming error in the plan layer, not user input, so it fails loudly rather
# than being silently skipped.
_HANDLERS: dict[str, _Handler] = {
    "enable-secrets-engine": _Handler(
        read=_read_secrets_engine,
        desired=_desired_secrets_engine,
        create=_create_secrets_engine,
        update=_tune_secrets_engine,
    ),
    "enable-auth-method": _Handler(
        read=_read_auth_method,
        desired=_desired_auth_method,
        create=_create_auth_method,
    ),
    "write-policy": _Handler(
        read=_read_policy,
        desired=_desired_policy,
        create=_write_policy,
        update=_write_policy,
    ),
    "configure-auth-method": _Handler(
        read=_read_path,
        desired=_desired_path,
        create=_path_write,
        update=_path_write,
    ),
    "write-auth-role": _Handler(
        read=_read_path,
        desired=_desired_path,
        create=_path_write,
        update=_path_write,
    ),
}


# --- comparing ---------------------------------------------------------------

_DURATION = re.compile(r"^([0-9]+)(s|m|h|d)?$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}

# Field names whose values are never printed, even though Ladon's config layer
# carries references rather than secret values.
_SENSITIVE_HINTS = ("secret", "password", "private")
_MAX_SHOWN = 60


def _normalise_hcl(hcl: str) -> str:
    """Policy HCL reduced to what it grants, so formatting is not drift.

    OpenBao returns a policy body as it was stored, but trailing whitespace and
    blank lines differ harmlessly between what a deployer wrote and what a
    previous Ladon version rendered.
    """
    lines = [line.strip() for line in str(hcl).splitlines()]
    return "\n".join(line for line in lines if line)


def _scalar(value):
    """A comparable form of a scalar.

    OpenBao echoes a duration back as an integer number of seconds ("1h" reads
    back as 3600), so durations compare by length rather than by text.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        match = _DURATION.match(value.strip())
        if match:
            return float(int(match.group(1)) * _UNIT_SECONDS[match.group(2) or "s"])
    return value


def _comparable(value):
    if isinstance(value, list | tuple):
        # Order is not significant for the list fields written here (policies,
        # audiences, redirect URIs) and OpenBao does not preserve it.
        return sorted(repr(_scalar(v)) for v in value)
    return _scalar(value)


def _matches(desired, live) -> bool:
    if live is _ABSENT:
        return False
    return _comparable(desired) == _comparable(live)


def _show(name: str, value) -> str | None:
    """A short single-line rendering of a value, or None if it must not be shown."""
    if value is _ABSENT:
        return "(absent)"
    if any(hint in name.lower() for hint in _SENSITIVE_HINTS):
        return None
    if isinstance(value, list | tuple):
        text = "[" + ", ".join(str(v) for v in value) + "]"
    else:
        text = str(value)
    if "\n" in text or len(text) > _MAX_SHOWN:
        return None
    return text


def _changes(desired: dict, live: dict) -> tuple[FieldChange, ...]:
    """The desired fields that the live object does not already satisfy.

    Only fields Ladon sets are compared. Anything else on the live object was put
    there by OpenBao's defaults or by an operator, and this plan has no opinion
    about it.
    """
    out: list[FieldChange] = []
    for name, want in desired.items():
        have = live.get(name, _ABSENT)
        if _matches(want, have):
            continue
        out.append(FieldChange(field=name, old=_show(name, have), new=_show(name, want)))
    return tuple(out)


def _assert_convergible(op: WriteOp, changes: tuple[FieldChange, ...]) -> None:
    blocked = [c for c in changes if c.field in _IMMUTABLE]
    if not blocked:
        return
    detail = ", ".join(c.render() for c in blocked)
    raise ApplyError(
        f"cannot converge {op.summary()}: {detail}. "
        "Changing this would destroy and recreate the object; remove it by hand first."
    )


def _diff_op(client, op: WriteOp) -> OpDiff:
    handler = _HANDLERS[op.kind]
    try:
        live = handler.read(client, op)
    except Exception as exc:  # noqa: BLE001 - re-raised as ApplyError with op context
        raise ApplyError(f"failed to read {op.summary()}: {exc}") from exc

    if live is None:
        return OpDiff(summary=op.summary(), action=CREATE)

    changes = _changes(handler.desired(op), live)
    if not changes:
        return OpDiff(summary=op.summary(), action=CURRENT)
    _assert_convergible(op, changes)
    if handler.update is None:
        raise ApplyError(
            f"cannot converge {op.summary()}: "
            f"{', '.join(c.render() for c in changes)} cannot be changed in place"
        )
    return OpDiff(summary=op.summary(), action=UPDATE, changes=changes)


def _write_op(client, op: WriteOp, diff: OpDiff) -> None:
    handler = _HANDLERS[op.kind]
    write = handler.create if diff.action == CREATE else handler.update
    try:
        write(client, op)
    except ApplyError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as ApplyError with op context
        # The op summary is value-free; the upstream message may not be, but
        # it comes from hvac/OpenBao and is the caller's to handle.
        raise ApplyError(f"failed to apply {op.summary()}: {exc}") from exc


def _verify_op(client, op: WriteOp) -> None:
    """Read the object back and confirm it matches what was written.

    OpenBao can accept a write and still not hold what was sent (a field the
    mount ignores, a policy that does not attach). Failing here names the op and
    the field, which is the difference between a caught misconfiguration and a
    silent one.
    """
    handler = _HANDLERS[op.kind]
    try:
        live = handler.read(client, op)
    except Exception as exc:  # noqa: BLE001 - re-raised as ApplyError with op context
        raise ApplyError(f"failed to read back {op.summary()}: {exc}") from exc

    if live is None:
        raise ApplyError(f"read-back of {op.summary()} found nothing: the write did not land")
    changes = _changes(handler.desired(op), live)
    if changes:
        detail = ", ".join(c.render() for c in changes)
        raise ApplyError(f"read-back of {op.summary()} does not match what was written: {detail}")


def supported_kinds() -> frozenset[str]:
    """The op kinds this executor knows how to apply."""
    return frozenset(_HANDLERS)


def _assert_executable(plan: Plan) -> None:
    """Fail before touching OpenBao if any op has no handler.

    Checking up front means a plan is applied all-or-nothing with respect to
    unknown kinds, rather than partway when it hits the first one.
    """
    unknown = sorted({op.kind for op in plan.ops if op.kind not in _HANDLERS})
    if unknown:
        raise ApplyError(f"no handler for op kind(s): {', '.join(unknown)}")


def apply_plan(plan: Plan, *, client=None, apply: bool = False) -> ApplyResult:
    """Diff a plan against live OpenBao, and converge to it when ``apply=True``.

    With a ``client``, every op is diffed against live state first, so a dry-run
    shows what would actually change and an apply writes only what differs. Each
    write is read back and verified before the next op runs, so a plan stops at
    the first op that did not land rather than reporting success.

    Without a ``client`` this is an offline dry-run: it touches nothing and
    returns the plan's op summaries as ``planned``. An apply requires a client.
    Payloads are never returned or logged.
    """
    _assert_executable(plan)

    if client is None:
        if apply:
            raise ApplyError("apply=True requires an authenticated hvac client")
        return ApplyResult(
            applied=(),
            planned=tuple(op.summary() for op in plan.ops),
            dry_run=True,
        )

    applied: list[str] = []
    diffs: list[OpDiff] = []
    for op in plan.ops:
        diff = _diff_op(client, op)
        diffs.append(diff)
        if not apply or not diff.needs_write:
            continue
        _write_op(client, op, diff)
        _verify_op(client, op)
        applied.append(op.summary())

    return ApplyResult(
        applied=tuple(applied),
        planned=() if apply else tuple(op.summary() for op in plan.ops),
        dry_run=not apply,
        diffs=tuple(diffs),
    )
