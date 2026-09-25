output "landing_bucket_name" {
  value = aws_s3_bucket.this["landing"].bucket
}

output "landing_bucket_arn" {
  value = aws_s3_bucket.this["landing"].arn
}

output "data_bucket_name" {
  value = aws_s3_bucket.this["data"].bucket
}

output "data_bucket_arn" {
  value = aws_s3_bucket.this["data"].arn
}

output "state_table_name" {
  value = aws_dynamodb_table.state.name
}

output "state_table_arn" {
  value = aws_dynamodb_table.state.arn
}
