"""Command-line entrypoint that wires the apply layer together (D3b).

This is the thin seam that makes the render -> plan -> execute -> connect layers
runnable end to end. Two subcommands:

* ``plan`` builds a plan from flags and renders it to stdout. It never connects
  to OpenBao, so it is always safe to run.
* ``apply`` builds the same plan, connects to OpenBao using the environment
  (address and token, or another credential), and applies it. It is a dry-run by
  default, per the secrets-hygiene rule that writes are opt-in; an explicit
  ``--apply`` is required to write.

Run as ``python -m ladon plan ...`` / ``python -m ladon apply ...``. Inputs are
explicit flags per phase; deriving a plan from a deployment config file is a
separate concern. Built on the standard library only (argparse), so the CLI adds
no dependency.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from ladon.apply.client import BaoConnection, BaoConnectionError, TokenAuth, connect
from ladon.apply.executor import ApplyError, apply_plan
from ladon.apply.plan import (
    DEFAULT_KV_MOUNT,
    Plan,
    root_config_plan,
    team_onboarding_plan,
)
from ladon.config.auth import jwt_auth, oidc_auth
from ladon.config.policies import baseline_team_policies, external_secrets_read
from ladon.config.teams import ConfigError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ladon",
        description="Render and apply OpenBao configuration plans (dry-run first).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("plan", "Render a plan to stdout without connecting to OpenBao."),
        ("apply", "Connect to OpenBao and apply a plan (dry-run unless --apply)."),
    ):
        p = sub.add_parser(name, help=help_text)
        _add_plan_flags(p)
        if name == "apply":
            p.add_argument(
                "--apply",
                action="store_true",
                help="Actually write to OpenBao. Without this flag, apply is a dry-run.",
            )
            p.add_argument(
                "--namespace",
                default=None,
                help="OpenBao namespace to scope the client to (for team onboarding).",
            )

    return parser


def _add_plan_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--phase",
        required=True,
        choices=("root-config", "team-onboarding"),
        help="Which plan to build.",
    )
    p.add_argument(
        "--kv-mount",
        default=DEFAULT_KV_MOUNT,
        help=f"KV v2 mount path (default: {DEFAULT_KV_MOUNT}).",
    )
    # root-config inputs
    p.add_argument(
        "--jwt-discovery-url",
        help="root-config: OIDC discovery URL for the workload JWT auth method.",
    )
    p.add_argument(
        "--oidc-discovery-url",
        help=(
            "root-config: OIDC discovery URL for the human OIDC auth method. "
            "team-onboarding: the cluster's OIDC discovery URL."
        ),
    )
    p.add_argument("--oidc-client-id", help="root-config: OIDC client id (public).")
    p.add_argument(
        "--oidc-client-secret-ref",
        help="root-config: reference the apply layer resolves to the OIDC client secret.",
    )
    # team-onboarding inputs
    p.add_argument("--cluster", help="team-onboarding: cluster name; mount is jwt-<cluster>.")


def build_plan(args: argparse.Namespace) -> Plan:
    """Build the plan selected by ``--phase`` from the parsed flags."""
    if args.phase == "root-config":
        return _build_root_config_plan(args)
    if args.phase == "team-onboarding":
        return _build_team_onboarding_plan(args)
    raise ConfigError(f"unknown phase {args.phase!r}")  # pragma: no cover - argparse guards


def _build_root_config_plan(args: argparse.Namespace) -> Plan:
    if not args.jwt_discovery_url:
        raise ConfigError("root-config requires --jwt-discovery-url")

    policies = [*baseline_team_policies(args.kv_mount), external_secrets_read(args.kv_mount)]

    auth_methods = [jwt_auth(discovery_url=args.jwt_discovery_url)]
    if args.oidc_discovery_url:
        missing = [
            flag
            for flag, value in (
                ("--oidc-client-id", args.oidc_client_id),
                ("--oidc-client-secret-ref", args.oidc_client_secret_ref),
            )
            if not value
        ]
        if missing:
            raise ConfigError(f"--oidc-discovery-url also requires {' and '.join(missing)}")
        auth_methods.append(
            oidc_auth(
                discovery_url=args.oidc_discovery_url,
                client_id=args.oidc_client_id,
                client_secret_ref=args.oidc_client_secret_ref,
            )
        )

    return root_config_plan(
        policies=policies,
        auth_methods=auth_methods,
        kv_mount=args.kv_mount,
    )


def _build_team_onboarding_plan(args: argparse.Namespace) -> Plan:
    if not args.cluster:
        raise ConfigError("team-onboarding requires --cluster")
    if not args.oidc_discovery_url:
        raise ConfigError("team-onboarding requires --oidc-discovery-url")
    return team_onboarding_plan(
        cluster=args.cluster,
        oidc_discovery_url=args.oidc_discovery_url,
        kv_mount=args.kv_mount,
    )


def _run_plan(args: argparse.Namespace, out) -> int:
    plan = build_plan(args)
    print(plan.render(), file=out)
    return 0


def _run_apply(args: argparse.Namespace, out) -> int:
    plan = build_plan(args)

    if not args.apply:
        # Dry-run: do not connect, just show what would be written.
        result = apply_plan(plan)
        print("DRY RUN: the following operations would be applied:", file=out)
        for line in result.planned:
            print(f"  {line}", file=out)
        print("\nRe-run with --apply to write to OpenBao.", file=out)
        return 0

    namespace = getattr(args, "namespace", None)
    conn = BaoConnection.from_env(namespace=namespace)
    client = connect(conn, TokenAuth.from_env())
    result = apply_plan(plan, client=client, apply=True)
    print("Applied the following operations:", file=out)
    for line in result.applied:
        print(f"  {line}", file=out)
    return 0


def main(argv: Sequence[str] | None = None, *, out=None) -> int:
    """Parse args and run the selected subcommand. Returns a process exit code."""
    out = out or sys.stdout
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return _run_plan(args, out)
        if args.command == "apply":
            return _run_apply(args, out)
    except (ConfigError, ApplyError, BaoConnectionError) as exc:
        # Expected, value-free failures: report and exit non-zero without a
        # traceback. The messages never include a secret.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0  # pragma: no cover - argparse requires a subcommand
