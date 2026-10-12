"""Command-line entrypoint that wires the apply layer together (D3b, D18).

This is the thin seam that makes the render -> plan -> execute -> connect layers
runnable end to end. Three subcommands:

* ``plan`` builds a plan from flags and renders it to stdout. It makes no
  connection, so it is always safe to run. It does read the OpenBao address for
  app-onboarding, because the store document has to name the server it dials,
  but it never contacts it, and it never contacts the cluster either: the
  documents it prints carry the current ESO custom-resource version rather than
  one probed from a cluster it is not allowed to dial.
* ``apply`` builds the same plan, connects to OpenBao using the environment
  (address and token, or another credential), and diffs it against live state.
  It is a dry-run by default, per the secrets-hygiene rule that writes are
  opt-in; an explicit ``--apply`` is required to write. The dry-run connects too
  (read-only), because a diff is only useful against what is really there; use
  ``plan`` when there is nothing to connect to.
* ``install-eso`` puts the External Secrets Operator on the cluster, or reports
  and reuses the one already running on it (D18a).
* ``seal-values`` renders the Helm values overlay that turns on cloud-KMS
  auto-unseal, from the deployment's ``kms`` block and the server config the
  deployment already runs. It prints the overlay and nothing else, so it pipes
  straight into a file or a ``helm upgrade -f -``; it reads two files and
  contacts nothing.
* ``audit`` connects read-only and reports every role and policy that grants
  more than it was meant to. It writes nothing, ever, so it needs no
  ``--apply``; it exits 3 when it finds something serious, which is what a
  scheduled run keys on.

Run as ``scripts/sero plan ...`` / ``scripts/sero apply ...`` (the wrapper puts
``src`` on the path; there is no installed console script). Inputs are explicit
flags per phase. A ``--config`` deployment file supplies the parts it already
models (the KV mount, the teams whose namespaces root config creates, and a
team's namespace via ``--team``); auth inputs stay explicit flags. Built on the
standard library only (argparse), so the CLI adds no dependency.

Two phases have a cluster half as well as (or instead of) an OpenBao one:
``app-onboarding`` renders the per-application service account and store (D17),
and ``sync`` renders one ``ExternalSecret`` pointing at an application's store.
``apply`` converges both halves in one command, OpenBao first: a store applied
against a role that was never written fails at refresh time, somewhere else,
later. The cluster half follows the same read-diff-write-verify rule, so a
second run reports every document already current and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from sero.apply.audit import (
    AUDIT_FINDINGS_EXIT,
    AuditError,
    AuditReport,
    audit_all,
    audit_namespace,
)
from sero.apply.client import (
    ADDRESS_ENV_VARS,
    BaoConnection,
    BaoConnectionError,
    TokenAuth,
    connect,
)
from sero.apply.executor import ApplyError, ApplyResult, apply_plan
from sero.apply.plan import (
    DEFAULT_KV_MOUNT,
    Plan,
    app_onboarding_plan,
    root_config_plan,
    team_onboarding_plan,
)
from sero.config import derive
from sero.config.apps import (
    APP_PATH_SEGMENT,
    DEFAULT_APP_AUDIENCE,
    DEFAULT_SA_NAMESPACE,
    AppIdentity,
    app_cluster_documents,
    app_identity,
    app_store_name,
    checked_app_path,
)
from sero.config.auth import jwt_auth, oidc_auth
from sero.config.eso import DEFAULT_API_VERSION, external_secret
from sero.config.loader import DeploymentConfig, load_deployment_config
from sero.config.policies import baseline_team_policies, external_secrets_read
from sero.config.seal import render_seal
from sero.config.seal_values import render_seal_values
from sero.config.teams import ConfigError
from sero.hygiene import has_url_userinfo
from sero.install.eso import DEFAULT_NAMESPACE as ESO_NAMESPACE
from sero.install.eso import ESOInstallResult, ESORelease, install_eso
from sero.install.helm import HelmError
from sero.install.k8s import CREATE as CLUSTER_CREATE
from sero.install.k8s import UPDATE as CLUSTER_UPDATE
from sero.install.k8s import (
    ApplyReport,
    ClusterApplyError,
    K8sError,
    converge_documents,
    detect_eso,
    eso_api_version,
)

# The default refresh interval on a rendered ExternalSecret: how often ESO
# re-reads the path. An hour is the ESO default and is frequent enough that a
# rotated secret reaches a workload the same working day.
DEFAULT_REFRESH_INTERVAL = "1h"

# Documents rendered for one phase, at the ESO API version the target cluster
# stores. A callable rather than a list because the version is not known until
# the cluster is probed, and ``plan`` never probes.
DocumentRenderer = Callable[[str], list[dict]]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sero",
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
                help=(
                    "Actually write to OpenBao and to the cluster. Without this "
                    "flag, apply is a dry-run."
                ),
            )

    eso = sub.add_parser(
        "install-eso",
        help="Install the External Secrets Operator, or reuse the one on the cluster.",
    )
    eso.add_argument(
        "--apply",
        action="store_true",
        help="Actually install. Without this flag, install-eso is a dry-run.",
    )
    eso.add_argument(
        "--namespace",
        default=ESO_NAMESPACE,
        help=(
            f"Namespace to install into. Defaults to {ESO_NAMESPACE}. An operator "
            "already running on the cluster is reused where it is, so this is "
            "ignored then."
        ),
    )

    seal_values = sub.add_parser(
        "seal-values",
        help="Render the Helm values overlay that turns on cloud-KMS auto-unseal.",
    )
    seal_values.add_argument(
        "--config",
        required=True,
        help=(
            "Path to a deployment config file. Its deployment.kms block names "
            "the provider and the key references the seal stanza is built from."
        ),
    )
    seal_values.add_argument(
        "--base-config",
        required=True,
        help=(
            "The server config the seal stanza is inserted into: either a Helm "
            "values file (its server.ha.raft.config is read; this is the usual "
            "case, and the file is one of the values files the deployment "
            "already installs with) or a plain HCL file. A .yaml or .yml "
            "suffix selects the values-file reading; anything else is read as "
            "HCL."
        ),
    )
    seal_values.add_argument(
        "--service-account-annotation",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help=(
            "Annotation to put on the OpenBao server's service account, "
            "repeatable. This is how the pod reaches the cloud KMS as an "
            "identity the key is granted to (workload identity on GKE, a role "
            "annotation on EKS); which key binds an identity is the cluster's "
            "convention, so it is given here rather than derived."
        ),
    )

    audit = sub.add_parser(
        "audit",
        help="Report roles and policies that grant more than they were meant to.",
    )
    audit.add_argument(
        "--config",
        default=None,
        help=(
            "Path to a deployment config file. Without --namespace, the audit "
            "covers the root and every team namespace the config names."
        ),
    )
    audit.add_argument(
        "--namespace",
        default=None,
        help="Audit only this OpenBao namespace. Without it, the root is audited.",
    )
    audit.add_argument(
        "--json",
        action="store_true",
        help="Print the findings as JSON instead of one line each.",
    )

    return parser


def _add_plan_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--phase",
        required=True,
        choices=("root-config", "team-onboarding", "app-onboarding", "sync"),
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
            "team-onboarding, app-onboarding and sync: team name; its namespace "
            "is resolved from --config, or defaults to the team name."
        ),
    )
    p.add_argument(
        "--teams",
        nargs="+",
        default=None,
        help=(
            "root-config: narrow the run to these teams. Their namespaces and "
            "the baseline inside them are applied, and nothing at the root, so "
            "the root's auth inputs are not needed."
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
    p.add_argument("--app", help="app-onboarding and sync: application name (a DNS-1123 label).")
    p.add_argument(
        "--k8s-namespace",
        default=None,
        help=(
            "app-onboarding: the one Kubernetes namespace allowed to use the "
            "application's store. sync: the namespace the ExternalSecret and "
            "its Secret live in; a namespace the store does not admit is "
            "refused by the store at refresh time, not here. Both default to "
            "the application name."
        ),
    )
    p.add_argument(
        "--sa-namespace",
        default=DEFAULT_SA_NAMESPACE,
        help=(
            "app-onboarding: the namespace holding the per-application service "
            f"accounts, which Sero owns. Defaults to {DEFAULT_SA_NAMESPACE}."
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
    # sync inputs
    p.add_argument(
        "--path",
        default=None,
        help=(
            "sync: the KV path the ExternalSecret reads, inside the "
            f"application's own prefix ({APP_PATH_SEGMENT}/<app>/<purpose>)."
        ),
    )
    p.add_argument(
        "--name",
        default=None,
        help="sync: the ExternalSecret's name.",
    )
    p.add_argument(
        "--target-name",
        default=None,
        help=(
            "sync: the Kubernetes Secret ESO creates, which is the name the "
            "consumer already uses. Defaults to --name."
        ),
    )
    p.add_argument(
        "--refresh-interval",
        default=DEFAULT_REFRESH_INTERVAL,
        help=f"sync: how often ESO re-reads the path. Defaults to {DEFAULT_REFRESH_INTERVAL}.",
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


def build_plan(args: argparse.Namespace, config: DeploymentConfig | None = None) -> Plan:
    """Build the plan selected by ``--phase`` from the flags and the config.

    ``config`` is passed in by the run paths, which load it once and use it for
    both halves of an onboarding; it is loaded here when a caller has none.
    """
    if config is None:
        config = _load_config(args)
    if args.phase == "root-config":
        return _build_root_config_plan(args, config)
    if args.phase == "team-onboarding":
        return _build_team_onboarding_plan(args, config)
    if args.phase == "app-onboarding":
        return app_onboarding_plan(build_app_identity(args, config))
    if args.phase == "sync":
        # Sync writes one cluster document and nothing in OpenBao: the identity
        # it reads through was written by app-onboarding.
        return Plan()
    raise ConfigError(f"unknown phase {args.phase!r}")  # pragma: no cover - argparse guards


def _build_root_config_plan(args: argparse.Namespace, config: DeploymentConfig | None) -> Plan:
    mount = _resolve_kv_mount(args, config)
    teams = config.teams if config else ()

    only_teams = getattr(args, "teams", None)
    if only_teams:
        # A narrowed run is one team's namespace and the baseline inside it.
        # Nothing at the root is planned, so the root's auth inputs are not
        # required: this is what an approved namespace request applies.
        return root_config_plan(
            policies=[],
            auth_methods=[],
            kv_mount=mount,
            teams=teams,
            only_teams=only_teams,
        )

    if not args.jwt_discovery_url:
        raise ConfigError("root-config requires --jwt-discovery-url")

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
        teams=teams,
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
    """Resolve one application's identity from the flags and the deployment config.

    The config is required, as it is for team onboarding. Everything this
    renders is keyed to the team's OpenBao namespace and the deployment's KV
    mount, and guessing either of them writes a policy and a role that look
    right and grant access to a path nothing uses.
    """
    if not args.team:
        raise ConfigError("app-onboarding requires --team")
    if not args.app:
        raise ConfigError("app-onboarding requires --app")
    if not args.cluster:
        raise ConfigError("app-onboarding requires --cluster")
    if config is None:
        raise ConfigError(
            "app-onboarding requires --config: the team's namespace and the KV "
            "mount come from the deployment config, not from a guess"
        )
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
    team's namespace through the config, so it comes from the one place it is
    defined rather than from a guess that would silently configure the wrong
    namespace.
    """
    explicit = getattr(args, "namespace", None)
    if explicit:
        return explicit
    team = getattr(args, "team", None)
    if not team:
        return None
    config = config if config is not None else _load_config(args)
    if config is None:
        raise ConfigError("--team requires --config to resolve the namespace")
    return derive.team_namespace(config, team)


def _build(
    args: argparse.Namespace, config: DeploymentConfig | None = None
) -> tuple[Plan, DocumentRenderer | None]:
    """The plan, and how to render the cluster documents for phases that have them.

    One place loads the config and builds the application identity, so both
    halves of an onboarding are rendered from exactly the same values. The
    documents come back as a renderer rather than a list because they carry the
    ESO custom-resource version the target cluster stores, and that is not known
    until the cluster is probed.

    ``config`` is the already-loaded deployment config, for a caller that has
    one (the backend, which never takes a config path from a request); it is
    loaded from ``--config`` when none is passed.
    """
    if config is None:
        config = _load_config(args)
    if args.phase == "sync":
        document = build_sync_document(args)
        return Plan(), lambda api_version: [_at_api_version(document, api_version)]
    if args.phase != "app-onboarding":
        return build_plan(args, config), None
    identity = build_app_identity(args, config)
    server = _resolve_server(args)

    def render(api_version: str) -> list[dict]:
        return app_cluster_documents(identity, server=server, api_version=api_version)

    return app_onboarding_plan(identity), render


def build_sync_document(args: argparse.Namespace) -> dict:
    """The one ``ExternalSecret`` the sync phase renders.

    The store is named from the team and the application through the same
    function that named it at onboarding, so a typo cannot produce a document
    that points at a store nothing created.
    """
    missing = [
        flag
        for flag, value in (
            ("--team", args.team),
            ("--app", args.app),
            ("--path", args.path),
            ("--name", args.name),
        )
        if not value
    ]
    if missing:
        raise ConfigError(f"sync requires {' and '.join(missing)}")
    return external_secret(
        name=args.name,
        # The same default onboarding uses, so the common case (one application,
        # one namespace of its own) needs no flag. A namespace the store does
        # not admit is refused by the store at refresh time, not here: which
        # namespaces a store admits is the store's to say.
        namespace=args.k8s_namespace or args.app,
        store_name=app_store_name(team=args.team, app=args.app),
        kv_path=checked_app_path(args.app, args.path, field="sync: --path"),
        secret_name=args.target_name or args.name,
        refresh_interval=args.refresh_interval,
    )


def _at_api_version(document: dict, api_version: str) -> dict:
    """The same document, addressed at the cluster's ESO resource version."""
    group = document["apiVersion"].rpartition("/")[0]
    return {**document, "apiVersion": f"{group}/{api_version}"}


def _resolve_server(args: argparse.Namespace) -> str:
    """The OpenBao address the rendered store dials.

    It is a location, not a credential, so reading it from the environment the
    apply already uses keeps one address in one place. An address carrying
    embedded credentials is refused rather than rendered: this value is written
    verbatim into a document that gets printed, reviewed and applied, and the
    error must not repeat what it found.
    """
    server = args.server
    if not server:
        server = next((os.environ[n] for n in ADDRESS_ENV_VARS if os.environ.get(n)), None)
    if not server:
        raise ConfigError(
            "app-onboarding renders a store that must name the OpenBao address: "
            f"pass --server or set {' or '.join(ADDRESS_ENV_VARS)}"
        )
    if has_url_userinfo(server):
        raise ConfigError(
            "the OpenBao address carries embedded credentials; give a plain "
            "address, and supply the credential through the environment"
        )
    return server


def _print_documents(documents: list[dict], out, *, note: str) -> None:
    """Print the cluster documents as YAML, with what happens to them."""
    print(f"\nCluster documents ({note}):", file=out)
    for document in documents:
        print("---", file=out)
        print(yaml.safe_dump(document, sort_keys=False).rstrip(), file=out)


def _run_plan(args: argparse.Namespace, out) -> int:
    plan, render = _build(args)
    print(plan.render(), file=out)
    if render is not None:
        # Offline by rule: the documents carry the current ESO resource version
        # rather than one probed from a cluster `plan` is not allowed to dial.
        # `apply` renders them again at whatever the target cluster stores.
        _print_documents(render(DEFAULT_API_VERSION), out, note="rendered, not applied")
    return 0


@dataclass(frozen=True)
class PhaseRun:
    """What one apply phase did, to OpenBao and to the cluster.

    ``openbao`` is None for a phase with no OpenBao half (sync). ``cluster`` is
    None for one with no cluster half, and also when the OpenBao half was
    blocked, because then nothing was sent to the cluster at all.

    ``error`` carries the one failure that happens *after* the cluster
    documents are written (the cluster did not store what was applied). It is
    reported rather than raised because the report has to travel with it: an
    operator, or a job row, that cannot see what landed has to go and look by
    hand. Every other failure raises.
    """

    openbao: ApplyResult | None = None
    documents: tuple[dict, ...] = ()
    cluster: ApplyReport | None = None
    dry_run: bool = True
    error: str | None = None

    @property
    def blocked(self) -> bool:
        """Whether anything in either half could not converge."""
        if self.openbao is not None and self.openbao.blocked:
            return True
        return bool(self.cluster is not None and self.cluster.blocked)


def run_phase(
    args: argparse.Namespace,
    *,
    config: DeploymentConfig | None = None,
    apply: bool = False,
    address: str | None = None,
) -> PhaseRun:
    """Run one phase's apply, both halves, and report what it did.

    The CLI and the backend's job runner both call this, so the ordering that
    makes an onboarding safe exists in one place. Both halves are rendered
    before anything is written: they describe one operation, so if the cluster
    half cannot be rendered nothing is written. The operator probe is part of
    that (D18a): a store applied to a cluster with no operator is a document
    nothing reads, and by the time that showed, the OpenBao half would already
    be written. OpenBao is converged first, and a blocked OpenBao half stops
    the cluster half from being applied at all: a store applied against a role
    that does not exist fails at refresh time, somewhere else, later.

    Both modes connect: a dry-run reads live state so the diff is against what
    is really there, and only ``apply`` writes. The client is built here, from
    ``address`` (or the environment) and the token in the environment, so no
    caller hands one in: a job runs on the deployment's own credential and
    never on something a request carried.
    """
    plan, render = _build(args, config)
    documents = tuple(render(_require_eso())) if render is not None else ()

    result = None
    if plan.ops:
        # The connection is not scoped: each op carries its namespace.
        client = connect(BaoConnection.from_env(address=address), TokenAuth.from_env())
        result = apply_plan(plan, client=client, apply=apply)
        if result.blocked:
            return PhaseRun(openbao=result, documents=documents, dry_run=not apply)

    if not documents:
        return PhaseRun(openbao=result, dry_run=not apply)

    try:
        report = converge_documents(documents, dry_run=not apply)
    except ClusterApplyError as exc:
        return PhaseRun(
            openbao=result,
            documents=documents,
            cluster=exc.report,
            dry_run=not apply,
            error=str(exc),
        )
    return PhaseRun(openbao=result, documents=documents, cluster=report, dry_run=not apply)


def run_eso_install(namespace: str = ESO_NAMESPACE, *, apply: bool = False) -> ESOInstallResult:
    """Install the External Secrets Operator, or reuse the one already there.

    The seam the CLI and the backend's job runner share, so both surfaces reuse
    an operator somebody else installed rather than fighting it for the CRDs.
    """
    return install_eso(ESORelease(namespace=namespace), apply=apply)


def _run_apply(args: argparse.Namespace, out) -> int:
    run = run_phase(args, apply=args.apply)

    if run.openbao is not None:
        _print_openbao_result(run.openbao, args, out)
    if run.openbao is not None and run.openbao.blocked:
        if run.documents:
            print("\nNothing was applied to the cluster: the OpenBao half did not land.", file=out)
        print(
            f"\n{len(run.openbao.blocked)} operation(s) blocked. Resolve them in OpenBao, "
            "then re-run.",
            file=out,
        )
        return 2

    if run.cluster is not None:
        _print_cluster_report(run.cluster, out, notes=_document_notes(run.documents))
    if run.error:
        # The documents were written before this failed, so what landed is
        # reported above before this line names what went wrong.
        print(f"error: {run.error}", file=sys.stderr)
        return 2
    if run.cluster is not None and run.cluster.blocked:
        print(
            f"\n{len(run.cluster.blocked)} cluster document(s) blocked. Resolve them on the "
            "cluster, then re-run.",
            file=out,
        )
        return 2

    if not args.apply:
        print(f"\nRe-run with --apply to write to {_targets(run)}.", file=out)
    return 0


def _run_install_eso(args: argparse.Namespace, out) -> int:
    """Install the External Secrets Operator, or reuse the one already there."""
    result = run_eso_install(args.namespace, apply=args.apply)
    print(result.summary, file=out)
    if result.api_version:
        print(
            f"The cluster stores the External Secrets resources at {result.api_version}.",
            file=out,
        )
    if not args.apply and not result.reused:
        print("\nRe-run with --apply to install.", file=out)
    return 0


def _base_server_config(path: str) -> str:
    """The server config the seal stanza is inserted into.

    A Helm values file is the usual input, because it is the file the deployment
    already installs with and the one whose value the overlay replaces; reading
    it here means the operator names that file rather than copying its HCL into
    a second one that can drift. A plain HCL file is accepted for a deployment
    that keeps its server config outside the values.
    """
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"base config file not found: {path}")
    text = file.read_text(encoding="utf-8")
    if file.suffix not in (".yaml", ".yml"):
        return text

    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    node = document
    for key in ("server", "ha", "raft", "config"):
        node = node.get(key) if isinstance(node, dict) else None
    if not isinstance(node, str) or not node.strip():
        raise ConfigError(
            f"{path} has no server.ha.raft.config to extend; point --base-config "
            "at the values file that carries the server config, or at a plain "
            "HCL file"
        )
    return node


def _service_account_annotations(values: Sequence[str]) -> dict[str, str]:
    """The ``KEY=VALUE`` annotation flags, parsed and checked.

    A malformed flag is refused rather than dropped: an annotation that silently
    did not land leaves the vault authenticating as the wrong identity, which
    shows up as a crash loop at startup and not here.
    """
    annotations: dict[str, str] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key.strip():
            raise ConfigError(f"--service-account-annotation expects KEY=VALUE, got {item!r}")
        annotations[key.strip()] = value.strip()
    return annotations


def _run_seal_values(args: argparse.Namespace, out) -> int:
    """Print the values overlay that installs the deployment's seal stanza.

    Nothing but the overlay reaches stdout, so the output is usable as a file
    without editing; refusals go to stderr with a non-zero exit.
    """
    config = load_deployment_config(args.config)
    kms = config.settings.get("kms")
    if kms is None:
        raise ConfigError(
            "the deployment config has no deployment.kms block, so there is no "
            "seal stanza to render; add one naming the provider and its key"
        )
    overlay = render_seal_values(
        _base_server_config(args.base_config),
        render_seal(kms),
        _service_account_annotations(args.service_account_annotation),
    )
    print(overlay, file=out, end="")
    return 0


# What a clean report does not cover when nothing told the audit which
# namespaces exist. Read without this line, "no findings" reads as "the
# deployment is clean", and every team namespace went unlooked at.
ROOT_ONLY_WARNING = (
    "only the root namespace was audited; pass --config to cover every team "
    "namespace the deployment has, or --namespace to name one"
)


def _audit_reports(args: argparse.Namespace, client) -> tuple[AuditReport, ...]:
    """Which namespaces to audit: the one named, or the root plus the config's.

    An explicit ``--namespace`` wins, as it does everywhere else in this CLI.
    Otherwise the deployment config names the team namespaces, so a scheduled
    audit covers the whole deployment without being told what is in it.
    """
    if args.namespace:
        return (audit_namespace(client, args.namespace),)
    config = _load_config(args)
    namespaces = [team.namespace for team in config.teams] if config else []
    return audit_all(client, namespaces)


def _run_audit(args: argparse.Namespace, out) -> int:
    """Audit roles and policies, read-only, and exit non-zero on a high finding."""
    client = connect(BaoConnection.from_env(), TokenAuth.from_env())
    reports = _audit_reports(args, client)
    ok = all(report.ok for report in reports)
    warning = ROOT_ONLY_WARNING if not (args.namespace or args.config) else None

    if args.json:
        payload = {"ok": ok, "namespaces": [report.as_dict() for report in reports]}
        if warning:
            payload["warning"] = warning
        print(json.dumps(payload, indent=2), file=out)
        return 0 if ok else AUDIT_FINDINGS_EXIT

    if warning:
        print(f"WARNING: {warning}.", file=out)
    if any(report.findings for report in reports):
        for report in reports:
            if report.findings:
                print(report.render(), file=out)
    else:
        print(f"No findings: {len(reports)} namespace(s) audited and clean.", file=out)

    # A distinct code, so a scheduled run can tell "found something" from
    # "could not run at all".
    return 0 if ok else AUDIT_FINDINGS_EXIT


def _require_eso() -> str:
    """The ESO resource version this cluster stores, or a refusal.

    ESO is what reads the store Sero renders, so onboarding an application onto
    a cluster with no operator produces documents nothing acts on. The version
    is probed rather than assumed, because a resource rendered at a version the
    cluster does not store is rejected on apply.
    """
    if detect_eso() is None:
        raise ConfigError(
            "no external-secrets operator is running on this cluster, so nothing "
            "would read the store this renders: install one first with "
            "`scripts/sero install-eso --apply`"
        )
    return eso_api_version()


def _targets(run: PhaseRun) -> str:
    """What a dry-run would have written to, named in the re-run line."""
    named = [
        name
        for name, used in (("OpenBao", run.openbao is not None), ("the cluster", run.documents))
        if used
    ]
    return " and ".join(named)


def _print_openbao_result(result, args: argparse.Namespace, out) -> None:
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


def _print_diffs(result, out) -> None:
    """One line per op: what it did (or would do) and which fields differ."""
    for diff in result.diffs:
        print(f"  {diff.render()}", file=out)


def _document_notes(documents: Sequence[dict]) -> dict[tuple[str, str | None, str], str]:
    """What a document does, for the documents whose target is not in the name.

    An ``ExternalSecret`` is the one that carries a secret somewhere: the report
    names the path it reads and the Secret it fills, never a value, so the
    dry-run shows what is about to be wired before it is wired.
    """
    notes = {}
    for document in documents:
        if document.get("kind") != "ExternalSecret":
            continue
        metadata = document.get("metadata", {})
        spec = document.get("spec", {})
        key = spec.get("dataFrom", [{}])[0].get("extract", {}).get("key", "")
        target = spec.get("target", {}).get("name", "")
        notes[("ExternalSecret", metadata.get("namespace"), metadata.get("name", ""))] = (
            f"{key} -> Secret/{target}"
        )
    return notes


def _print_cluster_report(report: ApplyReport, out, *, notes=None) -> None:
    """One line per document: what it did (or would do) to the cluster."""
    notes = notes or {}
    if report.dry_run:
        header = "Cluster documents (server dry run):"
    elif any(r.action in (CLUSTER_CREATE, CLUSTER_UPDATE) for r in report.results):
        header = "Cluster documents (applied):"
    elif report.blocked:
        header = "Cluster documents (nothing applied):"
    else:
        header = "Cluster documents: every document is already current."
    print(f"\n{header}", file=out)
    for result in report.results:
        line = f"  [cluster] {result.action} {result.target}"
        for detail in (notes.get((result.kind, result.namespace, result.name)), result.reason):
            if detail:
                line += f" ({detail})"
        print(line, file=out)


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
        if args.command == "install-eso":
            return _run_install_eso(args, out)
        if args.command == "seal-values":
            return _run_seal_values(args, out)
        if args.command == "audit":
            return _run_audit(args, out)
    except (ConfigError, ApplyError, AuditError, BaoConnectionError, K8sError, HelmError) as exc:
        # Expected, value-free failures: report and exit non-zero without a
        # traceback. The messages never include a secret.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0  # pragma: no cover - argparse requires a subcommand
