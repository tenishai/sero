"""Render one application's own secret identity (D17).

Per-team is not small enough. A team namespace holds every application a team
runs, so a single ESO identity for the team can read all of them, and one
compromised workload reads its neighbours' secrets. D17 puts the boundary at the
application: one path prefix (``<mount>/apps/<app>/...``), one policy scoped to
that prefix, one JWT role pinned by subject to a service account created for
that application alone, and one ``ClusterSecretStore`` admitted into exactly one
Kubernetes namespace.

Four pieces make that identity, and every one of them must agree or the login
fails closed:

* the policy, named ``eso-<app>-readonly``, inside the team's OpenBao namespace;
* the JWT role ``eso-<team>-<app>-<digest>`` on the cluster's ``jwt-<cluster>``
  mount, bound to the audience the store requests and to the service account's
  subject;
* the ``ServiceAccount eso-<team>-<app>-<digest>``, in Ladon's own namespace
  (D18a) rather than the operator's, so onboarding never writes into a
  namespace another team owns;
* the ``ClusterSecretStore ladon-<team>-<app>-<digest>``, which names the team's
  OpenBao namespace, the JWT mount, the role, and the service account to mint a
  token for.

The names are team-qualified wherever they are visible outside the team's
namespace (role, service account, store), because those live in one flat,
cluster-wide space: without the team in the name, one team's application could
claim the identity of another team's application of the same name. They also
carry a short digest of the pair, because joining two hyphenated labels with a
hyphen is ambiguous on its own (see :func:`_identity_digest`).

Rendering is pure: it returns a :class:`~ladon.config.policies.Policy`, an
:class:`~ladon.config.auth.AuthRole`, and plain manifest dicts. Nothing here
holds a secret value; the store carries references and the token is minted in
the cluster at runtime.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from ladon.config.auth import AuthRole, jwt_role
from ladon.config.eso import DEFAULT_API_VERSION, ClusterSecretStore, vault_store
from ladon.config.policies import DEFAULT_KV_MOUNT, Policy, path_block
from ladon.config.teams import ConfigError
from ladon.config.validate import require

# The audience the store requests and the role binds. Generic and explicit: a
# projected token is only accepted for the audience it was minted for, so this
# value must be the same on both halves (``app_identity`` guarantees that).
DEFAULT_APP_AUDIENCE = "openbao"

# Ladon's own Kubernetes namespace, where the per-application service accounts
# live (D18a). Not the operator's namespace: ESO is shared, and Ladon must not
# create identities inside a namespace another team administers.
DEFAULT_SA_NAMESPACE = "ladon"

# The one path prefix convention: <mount>/apps/<app>/<purpose>.
APP_PATH_SEGMENT = "apps"

# Kubernetes DNS-1123 label rules, which every name here must satisfy: the
# service account and namespaces are Kubernetes objects, and the role, policy
# and store names are built from the same parts, so one rule covers all of them.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_LABEL_MAX = 63

# How much of the digest goes into a name. Six hex is 24 bits: enough that two
# names a deployment actually holds do not collide, short enough to read and to
# type. It disambiguates; it is not a security boundary.
_DIGEST_LENGTH = 6


def _label(value: object, field: str) -> str:
    """Require a DNS-1123 label and return it, else raise ConfigError."""
    if not isinstance(value, str) or not value or len(value) > _LABEL_MAX:
        raise ConfigError(
            f"app onboarding: {field} {value!r} must be a lowercase DNS-1123 label "
            f"of at most {_LABEL_MAX} characters"
        )
    if not _LABEL_RE.match(value):
        raise ConfigError(
            f"app onboarding: {field} {value!r} must be a lowercase DNS-1123 label "
            "(a-z, 0-9, and hyphens, starting and ending alphanumeric)"
        )
    return value


def _namespace_path(value: object, field: str) -> str:
    """Require a namespace path: one or more labels joined by ``/``.

    OpenBao namespaces nest, so a team under a parent namespace is a legitimate
    path rather than a name. Every segment is still validated, so a path cannot
    smuggle an empty or malformed segment past the label rule.
    """
    if not isinstance(value, str) or not value:
        raise ConfigError(f"app onboarding: {field} {value!r} must be a non-empty namespace path")
    for segment in value.split("/"):
        _label(segment, field)
    return value


def _identity_digest(team: str, app: str) -> str:
    """A short digest of the (team, application) pair, to disambiguate names.

    ``eso-<team>-<app>`` is ambiguous on its own, because both parts may contain
    hyphens: team ``a-b`` with app ``c`` and team ``a`` with app ``b-c`` both
    read as ``a-b-c``. Those two live in different OpenBao namespaces but share
    one cluster, so the collision would give them the same service account, the
    same store, and the same bound subject. The null byte between the parts is
    what makes the digest unambiguous: it cannot occur in either label.
    """
    return hashlib.sha256(f"{team}\x00{app}".encode()).hexdigest()[:_DIGEST_LENGTH]


def _store_name(team: str, app: str) -> str:
    return f"ladon-{team}-{app}-{_identity_digest(team, app)}"


def app_store_name(*, team: str, app: str) -> str:
    """The ClusterSecretStore name for one application.

    The same function :attr:`AppIdentity.store_name` uses, validated the same
    way, so a caller that points an ``ExternalSecret`` at an application's store
    and the onboarding that created it cannot disagree about the name. Neither
    the cluster nor either namespace takes part in it, so a caller holding only
    the team and the application can compute it without guessing the rest.
    """
    return _store_name(_label(team, "team"), _label(app, "app"))


@dataclass(frozen=True)
class AppIdentity:
    """The resolved names for one application's ESO identity.

    Every renderer below takes this one value, so the policy, the role, the
    service account and the store cannot disagree about a name, a namespace or
    an audience. Build it with :func:`app_identity`, which validates the parts.
    """

    team: str
    app: str
    cluster: str
    namespace: str
    k8s_namespace: str
    sa_namespace: str = DEFAULT_SA_NAMESPACE
    audience: str = DEFAULT_APP_AUDIENCE
    kv_mount: str = DEFAULT_KV_MOUNT

    @property
    def digest(self) -> str:
        """The suffix that makes every cluster-wide name unambiguous.

        Derived from the team and the application and from nothing else, so the
        role, the service account and the store all carry the same one, and
        onboarding the same application twice never renames it.
        """
        return _identity_digest(self.team, self.app)

    @property
    def name(self) -> str:
        """The role and service account name, qualified by team and disambiguated."""
        return f"eso-{self.team}-{self.app}-{self.digest}"

    @property
    def policy_name(self) -> str:
        """The policy name. It lives inside the team's namespace already."""
        return f"eso-{self.app}-readonly"

    @property
    def store_name(self) -> str:
        return _store_name(self.team, self.app)

    @property
    def auth_mount(self) -> str:
        """The cluster's JWT mount, created once per cluster by team onboarding."""
        return f"jwt-{self.cluster}"

    @property
    def secret_prefix(self) -> str:
        """The KV path prefix this application owns, without the data/metadata part."""
        return f"{APP_PATH_SEGMENT}/{self.app}"

    @property
    def bound_subject(self) -> str:
        return f"system:serviceaccount:{self.sa_namespace}:{self.name}"


def app_identity(
    *,
    team: str,
    app: str,
    cluster: str,
    namespace: str | None = None,
    k8s_namespace: str | None = None,
    sa_namespace: str = DEFAULT_SA_NAMESPACE,
    audience: str = DEFAULT_APP_AUDIENCE,
    kv_mount: str = DEFAULT_KV_MOUNT,
) -> AppIdentity:
    """Resolve and validate the names for one application's identity.

    ``namespace`` is the team's OpenBao namespace, defaulting to the team name
    (the baseline convention, and what ``teams:`` defaults to) and allowed to be
    a nested path. ``k8s_namespace`` is the one Kubernetes namespace allowed to
    use the store, defaulting to the application's own name.
    """
    require(cluster, "cluster", context="app onboarding")
    require(audience, "audience", context="app onboarding")
    return AppIdentity(
        team=_label(team, "team"),
        app=_label(app, "app"),
        cluster=cluster,
        namespace=_namespace_path(namespace or team, "namespace"),
        k8s_namespace=_label(k8s_namespace or app, "k8s_namespace"),
        sa_namespace=_label(sa_namespace, "sa_namespace"),
        audience=audience,
        kv_mount=kv_mount,
    )


def eso_app_policy(identity: AppIdentity) -> Policy:
    """The read-only policy for one application's prefix, and nothing else.

    The KV v2 data path is granted twice: once for the secrets under the prefix
    and once for the prefix itself, because ``kv/data/apps/<app>/*`` does not
    match ``kv/data/apps/<app>``, and an application that keeps one secret at
    its own name would otherwise be unreadable.

    The four self paths are not a widening: the role sets
    ``token_no_default_policy``, which drops the ``default`` policy that
    normally grants them, and without them a token cannot look itself up, renew
    itself, revoke itself, or answer ESO's capability probe.
    """
    prefix = identity.secret_prefix
    mount = identity.kv_mount
    blocks = [
        path_block(f"{mount}/data/{prefix}/*", ["read"]),
        path_block(f"{mount}/data/{prefix}", ["read"]),
        path_block(f"{mount}/metadata/{prefix}/*", ["read", "list"]),
        path_block("auth/token/lookup-self", ["read"]),
        path_block("auth/token/renew-self", ["update"]),
        path_block("auth/token/revoke-self", ["update"]),
        path_block("sys/capabilities-self", ["update"]),
    ]
    return Policy.from_blocks(identity.policy_name, blocks)


def eso_app_role(identity: AppIdentity) -> AuthRole:
    """The JWT role for one application, pinned to its own service account.

    The audience alone proves nothing: any workload on the cluster can mint a
    token for a known audience. The subject is what ties the role to the one
    service account Ladon created for this application.
    """
    return jwt_role(
        identity.name,
        policies=[identity.policy_name],
        bound_audiences=[identity.audience],
        bound_subject=identity.bound_subject,
    )


def eso_app_service_account(identity: AppIdentity) -> dict:
    """The service account whose projected token the store exchanges for a login.

    ``automountServiceAccountToken`` is off: ESO projects an audience-scoped
    token for this account itself, so the default unaudienced token mounted into
    pods is a credential nothing here needs.
    """
    return {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": identity.name, "namespace": identity.sa_namespace},
        "automountServiceAccountToken": False,
    }


def eso_app_store(
    identity: AppIdentity, *, server: str, api_version: str = DEFAULT_API_VERSION
) -> ClusterSecretStore:
    """The ClusterSecretStore for one application, admitted into one namespace.

    Every field comes from the same identity as the role and the service
    account, so the store cannot request an audience the role does not bind or
    authenticate as an account the role does not trust.

    ``api_version`` is the ESO custom-resource version the target cluster
    stores. It defaults to the current one; a caller that has probed a cluster
    passes what it found, because a resource rendered at a version the cluster
    does not store is the API mismatch deployment-order.md warns about.
    """
    return vault_store(
        name=identity.store_name,
        server=server,
        auth_mount=identity.auth_mount,
        role=identity.name,
        audiences=[identity.audience],
        namespace=identity.namespace,
        k8s_namespaces=[identity.k8s_namespace],
        mount_path=identity.kv_mount,
        sa_name=identity.name,
        sa_namespace=identity.sa_namespace,
        api_version=api_version,
    )


def app_cluster_documents(
    identity: AppIdentity, *, server: str, api_version: str = DEFAULT_API_VERSION
) -> list[dict]:
    """The cluster half of app onboarding, in apply order.

    The service account comes first: the store names it, and ESO cannot mint a
    token for an account that does not exist yet.
    """
    return [
        eso_app_service_account(identity),
        eso_app_store(identity, server=server, api_version=api_version).manifest,
    ]
