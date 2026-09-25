terraform {
  required_version = ">= 1.16, < 2.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.66"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.8"
    }
  }

  # Bucket, key, region and use_lockfile are supplied by `make init` from the
  # bootstrap stack's outputs, so no account identifiers live in the repo.
  backend "s3" {}
}
