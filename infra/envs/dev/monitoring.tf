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

# Account spend guard (PLAN.md 5.3). Budgets with notifications only are free. The Free
# plan cannot be charged, so this catches credit burn before the plan ends early.
resource "aws_budgets_budget" "monthly_spend" {
  name         = "${local.name_prefix}-monthly-spend"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  # Measure gross usage. The default nets credits, and on a Free plan account every charge
  # is offset by a credit, so the budget read $0.00 by construction and could never alert
  # before the credits were gone (measured 2026-09-27: $0.031 gross, $0.00 net).
  cost_types {
    include_credit = false
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 20
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.alert_email]
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

# ------------------------------------------------------------------ dashboard (3 are free)

locals {
  sm_arn = module.orchestration.state_machine_arn
  function_names = [
    module.fn_validate_input.function_name,
    module.fn_enrich.function_name,
    module.fn_build_curated.function_name,
  ]
}

resource "aws_cloudwatch_dashboard" "pipeline" {
  dashboard_name = "${local.name_prefix}-pipeline"

  dashboard_body = jsonencode({
    widgets = [
      {
        type = "metric", x = 0, y = 0, width = 12, height = 6
        properties = {
          title = "Executions (per hour)", region = var.region, view = "timeSeries", stat = "Sum", period = 3600
          metrics = [
            ["AWS/States", "ExecutionsStarted", "StateMachineArn", local.sm_arn],
            ["AWS/States", "ExecutionsSucceeded", "StateMachineArn", local.sm_arn],
            ["AWS/States", "ExecutionsFailed", "StateMachineArn", local.sm_arn],
            ["AWS/States", "ExecutionsTimedOut", "StateMachineArn", local.sm_arn],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 0, width = 12, height = 6
        properties = {
          title       = "Execution time, max (ms)", region = var.region, view = "timeSeries", stat = "Maximum", period = 3600
          metrics     = [["AWS/States", "ExecutionTime", "StateMachineArn", local.sm_arn]]
          annotations = { horizontal = [{ label = "freshness objective", value = var.max_execution_seconds * 1000 }] }
        }
      },
      {
        type = "metric", x = 0, y = 6, width = 12, height = 6
        properties = {
          title = "Rows (per hour)", region = var.region, view = "timeSeries", stat = "Sum", period = 3600
          metrics = [
            ["PeopleEnrichment", "Matched", "service", "enrich"],
            ["PeopleEnrichment", "BudgetDeferred", "service", "enrich"],
            ["PeopleEnrichment", "Error", "service", "enrich"],
            ["PeopleEnrichment", "ProviderUnavailable", "service", "enrich"],
            ["PeopleEnrichment", "RowsInvalid", "service", "validate-input"],
            ["PeopleEnrichment", "PersonsCurated", "service", "build-curated"],
          ]
        }
      },
      {
        type = "metric", x = 12, y = 6, width = 12, height = 6
        properties = {
          title = "Credits: spent per hour and month-to-date vs ceilings", region = var.region, view = "timeSeries", period = 3600
          metrics = [
            ["PeopleEnrichment", "CreditsSpent", "service", "enrich", { stat = "Sum" }],
            ["PeopleEnrichment", "EnrichCreditsUsedThisMonth", "service", "enrich", { stat = "Maximum" }],
            ["PeopleEnrichment", "IdentifyCreditsUsedThisMonth", "service", "enrich", { stat = "Maximum" }],
          ]
          annotations = {
            horizontal = [
              { label = "enrich ceiling", value = var.max_enrich_credits },
              { label = "identify ceiling", value = var.max_identify_credits },
            ]
          }
        }
      },
      {
        type = "metric", x = 0, y = 12, width = 12, height = 6
        properties = {
          title   = "Lambda errors (per hour)", region = var.region, view = "timeSeries", stat = "Sum", period = 3600
          metrics = [for fn in local.function_names : ["AWS/Lambda", "Errors", "FunctionName", fn]]
        }
      },
      {
        type = "metric", x = 12, y = 12, width = 12, height = 6
        properties = {
          title = "Lambda duration p95 (ms) and DLQ depth", region = var.region, view = "timeSeries", period = 3600
          metrics = concat(
            [for fn in local.function_names : ["AWS/Lambda", "Duration", "FunctionName", fn, { stat = "p95" }]],
            [["AWS/SQS", "ApproximateNumberOfMessagesVisible", "QueueName", aws_sqs_queue.lambda_dlq.name, { stat = "Maximum", yAxis = "right" }]],
          )
        }
      },
    ]
  })
}

# ------------------------------------------------------------------ account guards ($0)

# External-access findings for this region's resources (public or cross-account access to
# buckets, roles, queues, KMS keys, ...). Free; findings reach the alerts topic.
resource "aws_accessanalyzer_analyzer" "external" {
  analyzer_name = "${local.name_prefix}-external-access"
  type          = "ACCOUNT"
}

resource "aws_cloudwatch_event_rule" "access_findings" {
  name        = "${local.name_prefix}-access-analyzer-findings"
  description = "Active IAM Access Analyzer findings (external access) to the alerts topic"

  event_pattern = jsonencode({
    source        = ["aws.access-analyzer"]
    "detail-type" = ["Access Analyzer Finding"]
    detail        = { status = ["ACTIVE"] }
  })
}

resource "aws_cloudwatch_event_target" "access_findings" {
  rule = aws_cloudwatch_event_rule.access_findings.name
  arn  = aws_sns_topic.alerts.arn

  input_transformer {
    input_paths = {
      resource = "$.detail.resource"
      type     = "$.detail.resourceType"
      isPublic = "$.detail.isPublic"
    }
    input_template = "\"IAM Access Analyzer: external access finding on <type> <resource> (public: <isPublic>). Review it in the IAM console.\""
  }
}

# EventBridge needs the topic's own policy to allow it; the alarms and the state machine
# already publish through the account-owner statement, which is kept as AWS creates it.
data "aws_iam_policy_document" "alerts_topic" {
  statement {
    sid    = "AccountOwner"
    effect = "Allow"

    principals {
      type        = "AWS"
      identifiers = ["*"]
    }

    actions = [
      "SNS:GetTopicAttributes",
      "SNS:SetTopicAttributes",
      "SNS:AddPermission",
      "SNS:RemovePermission",
      "SNS:DeleteTopic",
      "SNS:Subscribe",
      "SNS:ListSubscriptionsByTopic",
      "SNS:Publish",
    ]
    resources = [aws_sns_topic.alerts.arn]

    condition {
      test     = "StringEquals"
      variable = "AWS:SourceOwner"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }

  statement {
    sid    = "EventBridgeFindings"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    actions   = ["SNS:Publish"]
    resources = [aws_sns_topic.alerts.arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.access_findings.arn]
    }
  }
}

resource "aws_sns_topic_policy" "alerts" {
  arn    = aws_sns_topic.alerts.arn
  policy = data.aws_iam_policy_document.alerts_topic.json
}

# AWS Cost Anomaly Detection is free and new accounts get a default services monitor with
# a $100 / 40 % daily subscription. This one emails at $1 of impact, the right scale for
# a pipeline that should cost nothing.
resource "aws_ce_anomaly_subscription" "dollar" {
  count = var.cost_anomaly_monitor_arn == "" ? 0 : 1

  name             = "${local.name_prefix}-cost-anomaly-1usd"
  frequency        = "DAILY"
  monitor_arn_list = [var.cost_anomaly_monitor_arn]

  subscriber {
    type    = "EMAIL"
    address = var.alert_email
  }

  threshold_expression {
    dimension {
      key           = "ANOMALY_TOTAL_IMPACT_ABSOLUTE"
      values        = ["1"]
      match_options = ["GREATER_THAN_OR_EQUAL"]
    }
  }
}

# The GitHub OIDC roles are external access by design; archive their findings so only
# unexpected external access reaches the alerts topic.
resource "aws_accessanalyzer_archive_rule" "github_roles" {
  analyzer_name = aws_accessanalyzer_analyzer.external.analyzer_name
  rule_name     = "github-oidc-roles"

  filter {
    criteria = "resource"
    contains = ["role/${var.project}-github-"]
  }

  filter {
    criteria = "isPublic"
    eq       = ["false"]
  }
}
