# Outputs for the GCP unseal-key module.
#
# These are the shared unseal-key contract (see ../README.md): the values the
# OpenBao seal stanza needs. They map one-to-one onto the gcpckms fields the
# config layer renders (see src/ladon/config/seal.py): project, region, key_ring,
# crypto_key. No secret material is output.

output "provider" {
  description = "Seal provider key for the config layer."
  value       = "gcpckms"
}

output "seal_config" {
  description = <<-EOT
    The gcpckms seal parameters, keyed exactly as the config layer's
    deployment.kms block expects. Feed straight into render_seal.
  EOT
  value = {
    project    = var.project_id
    region     = var.location
    key_ring   = var.key_ring
    crypto_key = var.crypto_key
  }
}

output "crypto_key_id" {
  description = "Fully qualified id of the crypto key, for reference and auditing."
  value       = module.kms.keys[var.crypto_key]
}
