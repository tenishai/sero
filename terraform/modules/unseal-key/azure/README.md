# unseal-key/azure (planned)

Azure Key Vault auto-unseal key for OpenBao. Not yet implemented; this documents
the contract the module will satisfy so the GCP module and the config layer can
be built against it now.

When implemented, prefer the official `Azure/avm-res-keyvault-vault/azurerm`
Azure Verified Module (or the current official Key Vault module) over hand-rolled
`azurerm_key_vault*` resources, pinned to a stable version in `docs/versions.md`.

## Inputs (planned)

- `resource_group` / `location` - where the vault lives.
- `vault_name`, `key_name` - the Key Vault and key to create.
- `unseal_member` - the principal (a managed identity) to grant key use.

## Outputs (shared contract, see ../README.md)

- `provider` = `"azurekeyvault"`
- `seal_config` = `{ tenant_id, vault_name, key_name }` - the `azurekeyvault`
  fields `src/ladon/config/seal.py` expects in `deployment.kms`.
- `crypto_key_id` - the key's versionless id.

The OpenBao identity authenticates via a managed identity, so no client secret is
created or output (matching the seal renderer, which never renders that secret).
