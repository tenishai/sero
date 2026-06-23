# unseal-key/aws (planned)

AWS KMS auto-unseal key for OpenBao. Not yet implemented; this documents the
contract the module will satisfy so the GCP module and the config layer can be
built against it now.

When implemented, prefer the official `terraform-aws-modules/kms/aws` registry
module over hand-rolled `aws_kms_*` resources, pinned to a stable version in
`docs/versions.md`.

## Inputs (planned)

- `region` - AWS region for the key.
- `alias` / key name - the KMS key alias to create.
- `unseal_member` - the IAM principal (an IRSA role ARN) to grant key use.

## Outputs (shared contract, see ../README.md)

- `provider` = `"awskms"`
- `seal_config` = `{ region, kms_key_id }` - the `awskms` fields
  `src/ladon/config/seal.py` expects in `deployment.kms`.
- `crypto_key_id` - the key ARN.

The OpenBao identity authenticates via IRSA, so no secret is created or output.
