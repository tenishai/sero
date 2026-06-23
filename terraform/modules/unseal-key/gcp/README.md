# unseal-key/gcp

GCP Cloud KMS auto-unseal key for OpenBao. Provisions the keyring, the crypto
key, and the cryptoKeyEncrypterDecrypter IAM grant for the OpenBao identity, via
the official `terraform-google-modules/kms/google` module (pinned in
[docs/versions.md](../../../../docs/versions.md)).

The grant is part of this module deliberately: in the source platform it lived
outside the substrate Terraform, so auto-unseal could fail silently for want of
it. Here the key and its one required permission apply as a unit.

## Usage

```hcl
module "unseal_key" {
  source = "../../modules/unseal-key/gcp"

  project_id    = "my-project"
  location      = "us-east4"
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
    region: us-east4          # = seal_config.region
    key_ring: openbao         # = seal_config.key_ring
    crypto_key: unseal        # = seal_config.crypto_key
```

The OpenBao identity authenticates to KMS via Workload Identity, so this module
creates and exports no key material.
