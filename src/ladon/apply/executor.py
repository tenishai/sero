"""Execute an apply plan against a live OpenBao (D3b, the thin SDK wrapper).

The planning layer (``ladon.apply.plan``) is pure: it produces an ordered,
value-free :class:`~ladon.apply.plan.Plan`. This module is the only part that
talks to OpenBao. It mirrors ``ladon.install``: execution defaults to a dry-run,
and an apply must be requested explicitly with ``apply=True``, per the
secrets-hygiene rule that writes are dry-run by default.

Each :class:`~ladon.apply.plan.WriteOp` kind maps to one OpenBao API call. The
two enable/mount operations use hvac's typed system-backend helpers; the path
writes (auth config, auth roles, and anything later phases add) use the generic
``write_data`` so the plan layer stays the single source of truth for what a path
write contains.

A dry-run never opens a connection or touches OpenBao: it returns the plan's
value-free render so it can be reviewed. Only an apply constructs calls, and even
then this module never logs a payload, only the op summaries (kind and path).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ladon.apply.plan import Plan, WriteOp

# This module does not import hvac: the caller constructs and authenticates the
# ``hvac.Client`` and passes it in. That keeps the executor testable with a fake
# client and free of any connection concern, and leaves the hvac pin (pyproject,
# docs/versions.md) to the layer that builds the client.


class ApplyError(RuntimeError):
    """An op could not be applied, or the client is unusable."""


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of executing (or dry-running) a plan.

    ``applied`` is the summary line of each op that was sent to OpenBao; on a
    dry-run it is empty and ``planned`` carries every op summary instead. Neither
    list ever contains a payload.
    """

    applied: tuple[str, ...] = ()
    planned: tuple[str, ...] = field(default_factory=tuple)
    dry_run: bool = True


def _apply_enable_secrets_engine(client, op: WriteOp) -> None:
    # path is "sys/mounts/<mount>"; the mount name is the last segment.
    mount = op.path.rsplit("/", 1)[-1]
    options = op.data.get("options")
    client.sys.enable_secrets_engine(
        backend_type=op.data["type"],
        path=mount,
        options=options,
    )


def _apply_enable_auth_method(client, op: WriteOp) -> None:
    # path is "sys/auth/<mount>"; the mount path is the last segment.
    mount = op.path.rsplit("/", 1)[-1]
    client.sys.enable_auth_method(method_type=op.data["type"], path=mount)


def _apply_write_policy(client, op: WriteOp) -> None:
    # path is "sys/policies/acl/<name>"; the policy name is the last segment.
    name = op.path.rsplit("/", 1)[-1]
    client.sys.create_or_update_policy(name=name, policy=op.data["policy"])


def _apply_path_write(client, op: WriteOp) -> None:
    # auth config and roles are plain path writes; the op path is already the API
    # path hvac expects.
    client.write_data(op.path, data=op.data)


# Dispatch from a WriteOp.kind to the hvac call that performs it. A kind absent
# from this table is a programming error in the plan layer, not user input, so it
# fails loudly rather than being silently skipped.
_DISPATCH: dict[str, Callable[[object, WriteOp], None]] = {
    "enable-secrets-engine": _apply_enable_secrets_engine,
    "enable-auth-method": _apply_enable_auth_method,
    "write-policy": _apply_write_policy,
    "configure-auth-method": _apply_path_write,
    "write-auth-role": _apply_path_write,
}


def supported_kinds() -> frozenset[str]:
    """The op kinds this executor knows how to apply."""
    return frozenset(_DISPATCH)


def _assert_executable(plan: Plan) -> None:
    """Fail before touching OpenBao if any op has no handler.

    Checking up front means a plan is applied all-or-nothing with respect to
    unknown kinds, rather than partway when it hits the first one.
    """
    unknown = sorted({op.kind for op in plan.ops if op.kind not in _DISPATCH})
    if unknown:
        raise ApplyError(f"no handler for op kind(s): {', '.join(unknown)}")


def apply_plan(plan: Plan, *, client=None, apply: bool = False) -> ApplyResult:
    """Dry-run a plan, or apply it to OpenBao when ``apply=True``.

    Dry-run (the default) touches nothing and needs no client: it returns the
    plan's op summaries as ``planned``. With ``apply=True`` a ``client`` (an
    authenticated ``hvac.Client``) is required and each op is dispatched to its
    hvac call in plan order; the summaries of the ops sent are returned as
    ``applied``. Payloads are never returned or logged.
    """
    _assert_executable(plan)

    if not apply:
        return ApplyResult(
            applied=(),
            planned=tuple(op.summary() for op in plan.ops),
            dry_run=True,
        )

    if client is None:
        raise ApplyError("apply=True requires an authenticated hvac client")

    applied: list[str] = []
    for op in plan.ops:
        try:
            _DISPATCH[op.kind](client, op)
        except ApplyError:
            raise
        except Exception as exc:  # noqa: BLE001 - re-raised as ApplyError with op context
            # The op summary is value-free; the upstream message may not be, but
            # it comes from hvac/OpenBao and is the caller's to handle.
            raise ApplyError(f"failed to apply {op.summary()}: {exc}") from exc
        applied.append(op.summary())

    return ApplyResult(applied=tuple(applied), planned=(), dry_run=False)
