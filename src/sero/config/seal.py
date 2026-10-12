"""Render the OpenBao auto-unseal (seal) stanza from config (D2a).

Auto-unseal is parameterized per deployment so Sero runs on any cloud's KMS, or
on Shamir (manual unseal) with no cloud at all. This module renders the seal HCL
stanza for a chosen provider from plain parameters; injecting it into the server
config and installing is the Helm layer's job.

Multi-cloud by design: every supported provider (GCP Cloud KMS, AWS KMS, Azure
Key Vault) is a pluggable option selected by ``provider``, with Shamir as the
always-available no-cloud fallback. Adding a provider is adding a renderer here,
not changing the install layer.

Secret hygiene: the stanza carries key *references* (project, key ring, key id,
vault and key names), never key material. Azure's ``client_secret`` is the one
field that would be a secret; it is never rendered into the HCL. The preferred
Azure path is a managed identity (no credentials in config at all); when a
``client_id`` is configured, the matching client secret is supplied to OpenBao
out of band via its ``AZURE_CLIENT_SECRET`` environment variable, so it never
passes through Sero's rendering. GCP and AWS use workload identity or instance
roles, so their stanzas hold no secret at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from sero.config.teams import ConfigError
from sero.config.validate import require_all

# Provider keys accepted in config. "shamir" means no auto-unseal stanza.
GCP_CKMS = "gcpckms"
AWS_KMS = "awskms"
AZURE_KEY_VAULT = "azurekeyvault"
SHAMIR = "shamir"

SUPPORTED_PROVIDERS = frozenset({GCP_CKMS, AWS_KMS, AZURE_KEY_VAULT, SHAMIR})


@dataclass(frozen=True)
class SealConfig:
    """A rendered seal stanza for one provider.

    ``provider`` is the chosen provider key. ``hcl`` is the HCL stanza to inject
    into the server config, or an empty string for Shamir (no stanza). ``fields``
    is the value-free mapping that produced the stanza, for review and tests.
    """

    provider: str
    hcl: str
    fields: dict


def _stanza(seal_type: str, fields: dict) -> str:
    lines = [f'seal "{seal_type}" {{']
    for key, value in fields.items():
        lines.append(f'  {key} = "{value}"')
    lines.append("}")
    return "\n".join(lines) + "\n"


def _seal_context(provider: str) -> str:
    return f"{provider} seal"


def gcpckms_seal(params: dict) -> SealConfig:
    """GCP Cloud KMS auto-unseal. Authenticates via workload identity (no secret)."""
    fields = require_all(
        params, "project", "region", "key_ring", "crypto_key", context=_seal_context(GCP_CKMS)
    )
    return SealConfig(GCP_CKMS, _stanza(GCP_CKMS, fields), fields)


def awskms_seal(params: dict) -> SealConfig:
    """AWS KMS auto-unseal. Authenticates via an instance/IRSA role (no secret)."""
    fields = require_all(params, "region", "kms_key_id", context=_seal_context(AWS_KMS))
    return SealConfig(AWS_KMS, _stanza(AWS_KMS, fields), fields)


def azurekeyvault_seal(params: dict) -> SealConfig:
    """Azure Key Vault auto-unseal.

    ``tenant_id``, ``vault_name``, and ``key_name`` are required. Credentials are
    optional and preferred-absent: omit ``client_id`` to use a managed identity.
    When ``client_id`` is given, the matching client secret is NOT configured
    here; OpenBao reads it from its ``AZURE_CLIENT_SECRET`` environment variable,
    so no secret passes through this rendering. Optional ``environment`` and
    ``resource`` are passed through when set.
    """
    fields = require_all(
        params, "tenant_id", "vault_name", "key_name", context=_seal_context(AZURE_KEY_VAULT)
    )

    client_id = params.get("client_id")
    if client_id:
        fields["client_id"] = client_id
    for optional in ("environment", "resource"):
        if params.get(optional):
            fields[optional] = params[optional]

    return SealConfig(AZURE_KEY_VAULT, _stanza(AZURE_KEY_VAULT, fields), fields)


_RENDERERS = {
    GCP_CKMS: gcpckms_seal,
    AWS_KMS: awskms_seal,
    AZURE_KEY_VAULT: azurekeyvault_seal,
}


def render_seal(config: dict | None) -> SealConfig:
    """Render the seal stanza from a ``kms`` config mapping.

    ``config`` is the ``deployment.kms`` block: a ``provider`` key plus that
    provider's parameters. A missing config, or ``provider: shamir``, yields a
    Shamir result with an empty stanza (manual unseal, no cloud). An unknown
    provider is a config error rather than a silent fallback to Shamir, so a
    typo cannot quietly drop a deployment to manual unseal.
    """
    if not config:
        return SealConfig(SHAMIR, "", {})

    if not isinstance(config, dict):
        raise ConfigError("kms config must be a mapping")

    provider = config.get("provider", SHAMIR)
    if provider == SHAMIR:
        return SealConfig(SHAMIR, "", {})
    if provider not in _RENDERERS:
        known = ", ".join(sorted(SUPPORTED_PROVIDERS))
        raise ConfigError(f"unknown seal provider {provider!r}; supported: {known}")

    params = {k: v for k, v in config.items() if k != "provider"}
    return _RENDERERS[provider](params)
