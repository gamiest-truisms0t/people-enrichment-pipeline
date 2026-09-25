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

# Dead-letter queue for any asynchronous Lambda invocation that fails after retries
# and for EventBridge events that could not start the pipeline. Step Functions invokes
# the functions synchronously, so this is a safety net, not a hot path.
resource "aws_sqs_queue" "lambda_dlq" {
  name                      = "${local.name_prefix}-lambda-dlq"
  message_retention_seconds = 1209600 # 14 days
  sqs_managed_sse_enabled   = true
}

data "aws_iam_policy_document" "lambda_dlq" {
  statement {
    sid     = "EventBridgeDeadLetter"
    actions = ["sqs:SendMessage"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    resources = [aws_sqs_queue.lambda_dlq.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [module.orchestration.event_rule_arn]
    }
  }
}

resource "aws_sqs_queue_policy" "lambda_dlq" {
  queue_url = aws_sqs_queue.lambda_dlq.id
  policy    = data.aws_iam_policy_document.lambda_dlq.json
}

# Alerts: pipeline failures (from the state machine) and Lambda error alarms.
# Unencrypted on purpose: CloudWatch alarms cannot publish to a topic encrypted with
# the AWS-managed key, and a customer-managed key costs $1/month. The topic carries
# no registrant data, only batch ids, counts and S3 references.
resource "aws_sns_topic" "alerts" {
  name = "${local.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

module "orchestration" {
  source = "../../modules/orchestration"

  name_prefix = local.name_prefix
  function_arns = {
    validate_input = module.fn_validate_input.function_arn
    enrich         = module.fn_enrich.function_arn
    build_curated  = module.fn_build_curated.function_arn
  }
  landing_bucket_name   = module.storage.landing_bucket_name
  alerts_topic_arn      = aws_sns_topic.alerts.arn
  dead_letter_queue_arn = aws_sqs_queue.lambda_dlq.arn
  max_concurrency       = var.max_concurrency
  log_retention_days    = var.log_retention_days
}
