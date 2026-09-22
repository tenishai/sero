"""Command-line entrypoint that wires the apply layer together (D3b).

This is the thin seam that makes the render -> plan -> execute -> connect layers
runnable end to end. Two subcommands:

* ``plan`` builds a plan from flags and renders it to stdout. It never connects
  to OpenBao, so it is always safe to run.
* ``apply`` builds the same plan, connects to OpenBao using the environment
  (address and token, or another credential), and diffs it against live state.
  It is a dry-run by default, per the secrets-hygiene rule that writes are
  opt-in; an explicit ``--apply`` is required to write. The dry-run connects too
  (read-only), because a diff is only useful against what is really there; use
  ``plan`` when there is nothing to connect to.

Run as ``python -m ladon plan ...`` / ``python -m ladon apply ...``. Inputs are
explicit flags per phase. A ``--config`` deployment file supplies the parts it
already models (the KV mount, the teams whose namespaces root config creates,
and a team's namespace via ``--team``); auth inputs stay explicit flags. Built on
the standard library only (argparse), so the CLI adds no dependency.

The ``app-onboarding`` phase has two halves (D17): the OpenBao objects, which
are a plan like any other, and the Kubernetes documents, which are printed as
YAML. Applying the documents to a cluster is the install layer's job, not this
one's, so ``apply`` writes the OpenBao half and prints the other.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

import yaml

from ladon.apply.client import (
    ADDRESS_ENV_VARS,
    BaoConnection,
    BaoConnectionError,
    TokenAuth,
    connect,
)
from ladon.apply.executor import ApplyError, apply_plan
from ladon.apply.plan import (
    DEFAULT_KV_MOUNT,
    Plan,
    app_onboarding_plan,
    root_config_plan,
    team_onboarding_plan,
)
from ladon.config import derive
from ladon.config.apps import (
    DEFAULT_APP_AUDIENCE,
    DEFAULT_SA_NAMESPACE,
    AppIdentity,
    app_cluster_documents,
    app_identity,
)
from ladon.config.auth import jwt_auth, oidc_auth
from ladon.config.loader import DeploymentConfig, load_deployment_config
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

    return parser


def _add_plan_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--phase",
        required=True,
        choices=("root-config", "team-onboarding", "app-onboarding"),
        help="Which plan to build.",
    )
    p.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a deployment config file. Supplies the KV mount, the teams "
            "root-config creates namespaces for, and a team's namespace (via "
            "--team). Auth inputs stay explicit flags."
        ),
    )
    p.add_argument(
        "--namespace",
        default=None,
        help=(
            "OpenBao namespace the phase's operations run in. Overrides the "
            "namespace resolved from --team and --config."
        ),
    )
    p.add_argument(
        "--kv-mount",
        default=None,
        help=(
            f"KV v2 mount path. Overrides the config; defaults to {DEFAULT_KV_MOUNT} "
            "when neither is given."
        ),
    )
    p.add_argument(
        "--team",
        help=(
            "team-onboarding and app-onboarding: team name; its namespace is "
            "resolved from --config, or defaults to the team name."
        ),
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
    # team-onboarding and app-onboarding inputs
    p.add_argument(
        "--cluster",
        help="team-onboarding and app-onboarding: cluster name; the mount is jwt-<cluster>.",
    )
    # app-onboarding inputs
    p.add_argument("--app", help="app-onboarding: application name (a DNS-1123 label).")
    p.add_argument(
        "--k8s-namespace",
        default=None,
        help=(
            "app-onboarding: the one Kubernetes namespace allowed to use the "
            "application's store. Defaults to the application name."
        ),
    )
    p.add_argument(
        "--sa-namespace",
        default=DEFAULT_SA_NAMESPACE,
        help=(
            "app-onboarding: the namespace holding the per-application service "
            f"accounts, which Ladon owns. Defaults to {DEFAULT_SA_NAMESPACE}."
        ),
    )
    p.add_argument(
        "--audience",
        default=DEFAULT_APP_AUDIENCE,
        help=(
            "app-onboarding: the token audience the store requests and the role "
            f"binds. Defaults to {DEFAULT_APP_AUDIENCE}."
        ),
    )
    p.add_argument(
        "--server",
        default=None,
        help=(
            "app-onboarding: the OpenBao address the rendered store dials. "
            f"Defaults to {' or '.join(ADDRESS_ENV_VARS)}."
        ),
    )


def _load_config(args: argparse.Namespace) -> DeploymentConfig | None:
    """Load the deployment config if ``--config`` was given, else None."""
    if not args.config:
        return None
    return load_deployment_config(args.config)


def _resolve_kv_mount(args: argparse.Namespace, config: DeploymentConfig | None) -> str:
    """The KV mount: an explicit ``--kv-mount`` wins, then config, then default."""
    if args.kv_mount:
        return args.kv_mount
    if config is not None:
        return derive.kv_mount(config)
    return DEFAULT_KV_MOUNT


def build_plan(args: argparse.Namespace) -> Plan:
    """Build the plan selected by ``--phase`` from the flags and optional config."""
    config = _load_config(args)
    if args.phase == "root-config":
        return _build_root_config_plan(args, config)
    if args.phase == "team-onboarding":
        return _build_team_onboarding_plan(args, config)
    if args.phase == "app-onboarding":
        return app_onboarding_plan(build_app_identity(args, config))
    raise ConfigError(f"unknown phase {args.phase!r}")  # pragma: no cover - argparse guards


def _build_root_config_plan(args: argparse.Namespace, config: DeploymentConfig | None) -> Plan:
    if not args.jwt_discovery_url:
        raise ConfigError("root-config requires --jwt-discovery-url")

    mount = _resolve_kv_mount(args, config)
    policies = [*baseline_team_policies(mount), external_secrets_read(mount)]

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
        kv_mount=mount,
        # Every team in the config gets its namespace and the baseline inside
        # it. With no config there are no teams, and the root alone is planned.
        teams=config.teams if config else (),
    )


def _build_team_onboarding_plan(args: argparse.Namespace, config: DeploymentConfig | None) -> Plan:
    if not args.cluster:
        raise ConfigError("team-onboarding requires --cluster")
    if not args.oidc_discovery_url:
        raise ConfigError("team-onboarding requires --oidc-discovery-url")
    return team_onboarding_plan(
        cluster=args.cluster,
        oidc_discovery_url=args.oidc_discovery_url,
        kv_mount=_resolve_kv_mount(args, config),
        namespace=resolve_namespace(args, config),
    )


def build_app_identity(args: argparse.Namespace, config: DeploymentConfig | None) -> AppIdentity:
    """Resolve one application's identity from the flags and optional config."""
    if not args.team:
        raise ConfigError("app-onboarding requires --team")
    if not args.app:
        raise ConfigError("app-onboarding requires --app")
    if not args.cluster:
        raise ConfigError("app-onboarding requires --cluster")
    return app_identity(
        team=args.team,
        app=args.app,
        cluster=args.cluster,
        namespace=resolve_namespace(args, config),
        k8s_namespace=args.k8s_namespace,
        sa_namespace=args.sa_namespace,
        audience=args.audience,
        kv_mount=_resolve_kv_mount(args, config),
    )


def resolve_namespace(
    args: argparse.Namespace, config: DeploymentConfig | None = None
) -> str | None:
    """The OpenBao namespace this phase's operations run in.

    An explicit ``--namespace`` wins. Otherwise a ``--team`` resolves to the
    team's namespace, from the config where there is one, so it comes from the
    one place it is defined. For app-onboarding a config is optional: the
    baseline convention is that a team's namespace is its name, so the team name
    stands in. Team onboarding still requires the config, because it is the
    phase that bootstraps a team and a typo there would silently onboard the
    wrong namespace.
    """
    explicit = getattr(args, "namespace", None)
    if explicit:
        return explicit
    team = getattr(args, "team", None)
    if not team:
        return None
    config = config if config is not None else _load_config(args)
    if config is not None:
        return derive.team_namespace(config, team)
    if args.phase == "app-onboarding":
        return team
    raise ConfigError("--team requires --config to resolve the namespace")


def _cluster_documents(args: argparse.Namespace) -> list[dict]:
    """The Kubernetes half of app-onboarding, rendered from the same identity."""
    config = _load_config(args)
    identity = build_app_identity(args, config)
    return app_cluster_documents(identity, server=_resolve_server(args))


def _resolve_server(args: argparse.Namespace) -> str:
    """The OpenBao address the rendered store dials.

    It is a location, not a credential, so reading it from the environment the
    apply already uses keeps one address in one place.
    """
    if args.server:
        return args.server
    for name in ADDRESS_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    raise ConfigError(
        "app-onboarding renders a store that must name the OpenBao address: "
        f"pass --server or set {' or '.join(ADDRESS_ENV_VARS)}"
    )


def _print_documents(documents: list[dict], out, *, note: str) -> None:
    """Print the cluster documents as YAML, with what happens to them."""
    print(f"\nCluster documents ({note}):", file=out)
    for document in documents:
        print("---", file=out)
        print(yaml.safe_dump(document, sort_keys=False).rstrip(), file=out)


def _run_plan(args: argparse.Namespace, out) -> int:
    plan = build_plan(args)
    print(plan.render(), file=out)
    if args.phase == "app-onboarding":
        _print_documents(_cluster_documents(args), out, note="rendered, not applied")
    return 0


def _run_apply(args: argparse.Namespace, out) -> int:
    plan = build_plan(args)

    # Rendered before connecting: the two halves describe one onboarding, so if
    # the cluster half cannot be rendered nothing should be written at all.
    documents = _cluster_documents(args) if args.phase == "app-onboarding" else []

    # Both modes connect: a dry-run reads live state so the diff is against what
    # is really there. Only --apply writes. Use `plan` for an offline render.
    # The connection is not scoped: each op carries the namespace it runs in.
    conn = BaoConnection.from_env()
    client = connect(conn, TokenAuth.from_env())
    result = apply_plan(plan, client=client, apply=args.apply)

    if not args.apply:
        print("DRY RUN: diff against live OpenBao:", file=out)
    elif result.blocked:
        # Nothing was written: a plan that cannot fully converge is not applied
        # in part.
        print("Nothing was applied: the plan cannot converge as it stands.", file=out)
    elif result.applied:
        print("Applied the following operations:", file=out)
    else:
        print("Nothing to apply: every object is already current.", file=out)

    _print_diffs(result, out)

    if documents:
        # The cluster half is applied by the install layer, not here. Printing
        # it keeps one command the whole description of an onboarding.
        _print_documents(
            documents, out, note="rendered, not applied: the cluster layer applies these"
        )

    if result.blocked:
        print(
            f"\n{len(result.blocked)} operation(s) blocked. Resolve them in OpenBao, then re-run.",
            file=out,
        )
        return 2
    if not args.apply:
        print("\nRe-run with --apply to write to OpenBao.", file=out)
    return 0


def _print_diffs(result, out) -> None:
    """One line per op: what it did (or would do) and which fields differ."""
    for diff in result.diffs:
        print(f"  {diff.render()}", file=out)


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
