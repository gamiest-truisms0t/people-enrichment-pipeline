output "state_bucket" {
  description = "Bucket that holds Terraform state for every environment stack."
  value       = aws_s3_bucket.state.bucket
}

output "region" {
  value = var.region
}
