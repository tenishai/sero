# Inputs for the GCP Cloud KMS unseal-key module.
#
# This module provisions the KMS key OpenBao uses for auto-unseal and grants the
# unsealing identity permission to use it. It carries no secret material: the
# identity authenticates via Workload Identity, so no service-account key is
# created or exported.

variable "project_id" {
  description = "GCP project that holds the KMS keyring and key."
  type        = string
}

variable "location" {
  description = "KMS location (region or multi-region) for the keyring, for example us-east4."
  type        = string
}

variable "key_ring" {
  description = "Name of the KMS keyring to create."
  type        = string
}

variable "crypto_key" {
  description = "Name of the KMS crypto key to create for auto-unseal."
  type        = string
}

variable "unseal_member" {
  description = <<-EOT
    IAM member granted use of the unseal key, in the form expected by Google IAM,
    for example "serviceAccount:bao@PROJECT.iam.gserviceaccount.com". This is the
    identity the OpenBao pods run as (via Workload Identity). The module grants it
    cryptoKeyEncrypterDecrypter on the key, so auto-unseal does not fail silently
    for want of the grant.
  EOT
  type        = string
}

variable "rotation_period" {
  description = "Automatic rotation period for the crypto key. Defaults to 90 days, matching the KMS module default."
  type        = string
  default     = "7776000s"
}

variable "labels" {
  description = "Labels to apply to the keyring's crypto key."
  type        = map(string)
  default     = {}
}
