# Pinned Terraform and provider versions for the GCP unseal-key module.
# Keep these in sync with docs/versions.md; bumps are their own reviewed PR.
terraform {
  required_version = ">= 1.15.6"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "7.37.0"
    }
  }
}
