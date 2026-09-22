# unseal-key/gcp

GCP Cloud KMS auto-unseal key for OpenBao. Provisions the keyring, the crypto
key, and the IAM grants the OpenBao identity needs on it, via the official
`terraform-google-modules/kms/google` module (pinned in
[docs/versions.md](../../../../docs/versions.md)).

Two roles, both on the key and neither on the project:

- `roles/cloudkms.cryptoKeyEncrypterDecrypter`, to wrap and unwrap the root key.
- `roles/cloudkms.viewer`, because the `gcpckms` seal calls `cryptoKeys.get` at
  startup to check the key exists. Encrypt/decrypt does not grant that read, and
  without it the vault fails with `error checking key existence` and never
  unseals.

The grants are part of this module deliberately: in the source platform they
lived outside the substrate Terraform, so auto-unseal could fail silently for
want of one. Here the key and the permissions it needs apply as a unit.

## Usage

```hcl
module "unseal_key" {
  source = "../../modules/unseal-key/gcp"

  project_id    = "my-project"
  location      = "<region>"
  key_ring      = "openbao"
  crypto_key    = "unseal"
  unseal_member = "serviceAccount:bao@my-project.iam.gserviceaccount.com"
}
```

The `seal_config` output feeds the config layer directly. With the value written
to a deployment config's `deployment.kms` block, `src/ladon/config/seal.py`
renders the matching OpenBao `seal "gcpckms"` stanza:

```
deployment:
  kms:
    provider: gcpckms
    project: my-project       # = seal_config.project
    region: <region>          # = seal_config.region
    key_ring: openbao         # = seal_config.key_ring
    crypto_key: unseal        # = seal_config.crypto_key
```

The OpenBao identity authenticates to KMS via Workload Identity, so this module
creates and exports no key material.
