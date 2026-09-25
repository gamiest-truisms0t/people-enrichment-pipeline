data "aws_caller_identity" "current" {}

# The AWS-managed key that SSM SecureString parameters use by default.
data "aws_kms_alias" "ssm" {
  name = "alias/aws/ssm"
}

module "storage" {
  source = "../../modules/storage"

  name_prefix        = local.name_prefix
  account_id         = data.aws_caller_identity.current.account_id
  force_destroy      = var.force_destroy_buckets
  raw_retention_days = var.raw_retention_days
}

module "secrets" {
  source = "../../modules/secrets"

  name_prefix = local.name_prefix
}

# Dead-letter queue for any asynchronous Lambda invocation that fails after retries.
# Step Functions invokes the functions synchronously, so this is a safety net, not a
# hot path.
resource "aws_sqs_queue" "lambda_dlq" {
  name                      = "${local.name_prefix}-lambda-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}
