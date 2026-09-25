# The provider API key lives in SSM Parameter Store as a SecureString encrypted
# with the AWS-managed aws/ssm key (no key fee). Terraform creates the parameter
# with a placeholder and never manages the value, so the real key is neither in
# Terraform state nor in git. Set it with `make set-api-key`.

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
  name        = "/${var.name_prefix}/pdl_api_key"
  description = "People Data Labs API key (value set out of band; Terraform ignores changes)"
  type        = "SecureString"
  tier        = "Standard"
  value       = "PLACEHOLDER-run-make-set-api-key"

  lifecycle {
    ignore_changes = [value]
  }
}
