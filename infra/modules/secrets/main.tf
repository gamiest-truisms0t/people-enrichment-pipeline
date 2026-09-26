# The provider API key lives in SSM Parameter Store as a SecureString encrypted
# with the AWS-managed aws/ssm key (no key fee). Terraform creates the parameter
# with a placeholder through the provider's write-only argument, so the value is
# never stored in Terraform state (a plain `value` would be: the provider reads
# SecureStrings back decrypted on every refresh). The real key is set out of band
# with `make set-api-key`; bumping value_wo_version would overwrite it with the
# placeholder again, so leave it at 1.

terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

resource "aws_ssm_parameter" "pdl_api_key" {
  name             = "/${var.name_prefix}/pdl_api_key"
  description      = "People Data Labs API key (value set out of band with make set-api-key)"
  type             = "SecureString"
  tier             = "Standard"
  value_wo         = "PLACEHOLDER-run-make-set-api-key"
  value_wo_version = 1
}
