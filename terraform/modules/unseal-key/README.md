# unseal-key modules

Provision the cloud KMS key OpenBao uses for auto-unseal (D2a), and grant the
OpenBao identity permission to use it. There is one module per cloud; they share
a single input/output contract so the rest of Sero does not care which cloud is
underneath. This is the multi-cloud rule applied to the substrate: pluggable from
the start, not one cloud generalized later.

The substrate layer (Terraform) owns these. Their output feeds the config layer:
the `seal_config` output maps one-to-one onto the `deployment.kms` block that
`src/sero/config/seal.py` renders into the OpenBao seal stanza.

Modules prefer the official cloud-provider registry modules over hand-rolled
resource blocks, pinned to a stable version in `docs/versions.md`.

## Shared contract

Every `unseal-key/<cloud>` module exposes these outputs:

| Output | Meaning |
| --- | --- |
| `provider` | The seal provider key: `gcpckms`, `awskms`, or `azurekeyvault`. |
| `seal_config` | A map of exactly the fields that provider needs in `deployment.kms`, ready to feed `render_seal`. No secret material. |
| `crypto_key_id` | The fully qualified key identifier, for reference and auditing. |

Each module takes the cloud's project/account and location, the key names to
create, and the `unseal_member` identity to grant key use to. None outputs secret
material: the OpenBao identity authenticates via that cloud's workload identity
(Workload Identity, IRSA, or a managed identity), so no key is exported.

## Status

| Cloud | Module | State |
| --- | --- | --- |
| GCP | `gcp/` | Implemented (Cloud KMS, via `terraform-google-modules/kms/google`). |
| AWS | `aws/` | Planned. Contract documented; implementation to follow. |
| Azure | `azure/` | Planned. Contract documented; implementation to follow. |
