# GCP Cloud KMS auto-unseal key for OpenBao.
#
# Provisions the keyring, the crypto key, and the IAM grant that lets the
# OpenBao identity use the key. The grant is part of this module on purpose: in
# the source platform the cryptoKeyEncrypterDecrypter grant lived outside the
# substrate Terraform, so a cluster could come up and fail auto-unseal silently.
# Keeping it with the key makes the key and its one required permission a single
# applied unit.
#
# Uses the official terraform-google-modules/kms/google registry module rather
# than hand-rolled google_kms_* resources, pinned in docs/versions.md. The module
# handles the keyring, key (with rotation and prevent_destroy), and the
# encrypter/decrypter IAM bindings.

resource "google_project_service" "kms" {
  project            = var.project_id
  service            = "cloudkms.googleapis.com"
  disable_on_destroy = false
}

module "kms" {
  source  = "terraform-google-modules/kms/google"
  version = "4.1.2"

  project_id = var.project_id
  location   = var.location
  keyring    = var.key_ring
  keys       = [var.crypto_key]

  # Grant the OpenBao identity encrypt+decrypt on the unseal key. Auto-unseal
  # needs both, so the member is set as encrypter and decrypter for the key.
  set_encrypters_for = [var.crypto_key]
  set_decrypters_for = [var.crypto_key]
  encrypters         = [var.unseal_member]
  decrypters         = [var.unseal_member]

  # The unseal key must outlive the config: losing it means losing the ability
  # to unseal. The module defaults prevent_destroy to true; we keep that.
  prevent_destroy = true

  # Rotation is optional; the module defaults to 90 days when unset here.
  key_rotation_period = var.rotation_period
  labels              = var.labels

  depends_on = [google_project_service.kms]
}
