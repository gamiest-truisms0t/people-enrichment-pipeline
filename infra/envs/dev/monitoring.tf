# Cross-cutting alarms. Lambda error alarms live in the lambda_function module and the
# state-machine alarms in the orchestration module; these watch the credit budget and the
# dead-letter queue. Eight alarms in total, inside the always-free ten.

locals {
  enrich_alarm_threshold   = ceil(var.max_enrich_credits * var.budget_alarm_fraction)
  identify_alarm_threshold = ceil(var.max_identify_credits * var.budget_alarm_fraction)
  # Round rather than floor: 0.29 * 100 is 28.999... in binary floating point.
  budget_alarm_pct = floor(var.budget_alarm_fraction * 100 + 0.5)
}

# The enrich function publishes the month-to-date credit counters read back from
# DynamoDB after every lookup (Powertools EMF, dimension service=enrich). The counters
# are gauges, so missing data is ignored: the alarm keeps its last state between batches
# instead of dropping back to OK five minutes after every run, and it clears on its own
# when the first batch of a new month publishes a low value.
resource "aws_cloudwatch_metric_alarm" "enrich_credit_budget" {
  alarm_name          = "${local.name_prefix}-enrich-credits-${local.budget_alarm_pct}pct"
  alarm_description   = "Enrichment credits used this month reached ${local.enrich_alarm_threshold} of the ${var.max_enrich_credits} ceiling."
  namespace           = "PeopleEnrichment"
  metric_name         = "EnrichCreditsUsedThisMonth"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.enrich_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "ignore"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    service = "enrich"
  }
}

resource "aws_cloudwatch_metric_alarm" "identify_credit_budget" {
  alarm_name          = "${local.name_prefix}-identify-credits-${local.budget_alarm_pct}pct"
  alarm_description   = "Identify credits used this month reached ${local.identify_alarm_threshold} of the ${var.max_identify_credits} ceiling (the free plan grants 5)."
  namespace           = "PeopleEnrichment"
  metric_name         = "IdentifyCreditsUsedThisMonth"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = local.identify_alarm_threshold
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "ignore"
  alarm_actions       = [aws_sns_topic.alerts.arn]

  dimensions = {
    service = "enrich"
  }
}

# Anything on the dead-letter queue means an asynchronous invocation or an EventBridge
# delivery failed after retries and needs a human.
resource "aws_cloudwatch_metric_alarm" "dead_letter_queue" {
  alarm_name          = "${local.name_prefix}-dead-letter-queue-not-empty"
  alarm_description   = "Messages are waiting on the pipeline dead-letter queue."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]

  dimensions = {
    QueueName = aws_sqs_queue.lambda_dlq.name
  }
}
