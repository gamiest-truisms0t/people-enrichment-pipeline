output "state_bucket" {
  description = "Bucket that holds Terraform state for every environment stack."
  value       = aws_s3_bucket.state.bucket
}

output "region" {
  value = var.region
}

output "github_role_arns" {
  description = "OIDC roles for GitHub Actions: plan (pull requests), readonly (drift), apply (main)."
  value       = { for k, r in aws_iam_role.github : k => r.arn }
}
