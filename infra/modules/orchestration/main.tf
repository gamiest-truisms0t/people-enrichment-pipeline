# Orchestration: the Step Functions workflow that runs a batch, the EventBridge rule
# that starts it when a CSV lands in the landing bucket, and their IAM roles.
# The alerts SNS topic is created by the caller so Lambda alarms can share it.

terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

# ------------------------------------------------------------------ state machine

resource "aws_cloudwatch_log_group" "state_machine" {
  name              = "/aws/vendedlogs/states/${var.name_prefix}-pipeline"
  retention_in_days = var.log_retention_days
}

data "aws_iam_policy_document" "state_machine_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["states.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "state_machine" {
  name               = "${var.name_prefix}-pipeline-sfn"
  assume_role_policy = data.aws_iam_policy_document.state_machine_assume.json
}

data "aws_iam_policy_document" "state_machine" {
  statement {
    sid       = "InvokeFunctions"
    actions   = ["lambda:InvokeFunction"]
    resources = values(var.function_arns)
  }

  statement {
    sid       = "Notify"
    actions   = ["sns:Publish"]
    resources = [var.alerts_topic_arn]
  }

  statement {
    sid       = "WriteExecutionLogs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.state_machine.arn}:*"]
  }

  # Log-delivery management and X-Ray have no resource-level permissions.
  statement {
    sid = "ManageLogDelivery"
    actions = [
      "logs:CreateLogDelivery",
      "logs:GetLogDelivery",
      "logs:UpdateLogDelivery",
      "logs:DeleteLogDelivery",
      "logs:ListLogDeliveries",
      "logs:PutResourcePolicy",
      "logs:DescribeResourcePolicies",
      "logs:DescribeLogGroups",
    ]
    resources = ["*"]
  }

  statement {
    sid = "Tracing"
    actions = [
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "state_machine" {
  name   = "pipeline"
  role   = aws_iam_role.state_machine.id
  policy = data.aws_iam_policy_document.state_machine.json
}

resource "aws_sfn_state_machine" "pipeline" {
  # checkov:skip=CKV_AWS_285: execution history IS logged (level ALL); state input/output is excluded from the log because rows carry names and emails
  name     = "${var.name_prefix}-pipeline"
  role_arn = aws_iam_role.state_machine.arn
  type     = "STANDARD"

  definition = templatefile("${path.module}/pipeline.asl.tftpl", {
    validate_input_arn = var.function_arns["validate_input"]
    enrich_arn         = var.function_arns["enrich"]
    build_curated_arn  = var.function_arns["build_curated"]
    alerts_topic_arn   = var.alerts_topic_arn
    max_concurrency    = var.max_concurrency
  })

  logging_configuration {
    log_destination        = "${aws_cloudwatch_log_group.state_machine.arn}:*"
    include_execution_data = var.log_execution_data
    level                  = "ALL"
  }

  tracing_configuration {
    enabled = true
  }

  depends_on = [aws_iam_role_policy.state_machine]
}

# ------------------------------------------------------------------ S3 upload trigger

data "aws_iam_policy_document" "events_assume" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "events" {
  name               = "${var.name_prefix}-pipeline-trigger"
  assume_role_policy = data.aws_iam_policy_document.events_assume.json
}

data "aws_iam_policy_document" "events" {
  statement {
    sid       = "StartPipeline"
    actions   = ["states:StartExecution"]
    resources = [aws_sfn_state_machine.pipeline.arn]
  }
}

resource "aws_iam_role_policy" "events" {
  name   = "start-pipeline"
  role   = aws_iam_role.events.id
  policy = data.aws_iam_policy_document.events.json
}

resource "aws_cloudwatch_event_rule" "csv_uploaded" {
  name        = "${var.name_prefix}-csv-uploaded"
  description = "Start the enrichment pipeline when a CSV lands under incoming/ in the landing bucket"

  event_pattern = jsonencode({
    source        = ["aws.s3"]
    "detail-type" = ["Object Created"]
    detail = {
      bucket = { name = [var.landing_bucket_name] }
      object = { key = [{ wildcard = "incoming/*.csv" }] }
    }
  })
}

resource "aws_cloudwatch_event_target" "state_machine" {
  rule     = aws_cloudwatch_event_rule.csv_uploaded.name
  arn      = aws_sfn_state_machine.pipeline.arn
  role_arn = aws_iam_role.events.arn

  # The state machine input is always {"bucket": ..., "key": ...}, whether it is
  # started by this rule or by hand; the rule adds the object version so the pipeline
  # can recognise a duplicate delivery of the same upload.
  input_transformer {
    input_paths = {
      bucket     = "$.detail.bucket.name"
      key        = "$.detail.object.key"
      version_id = "$.detail.object.version-id"
      etag       = "$.detail.object.etag"
    }
    input_template = "{\"bucket\": <bucket>, \"key\": <key>, \"version_id\": <version_id>, \"etag\": <etag>}"
  }

  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 10
  }

  dead_letter_config {
    arn = var.dead_letter_queue_arn
  }
}

# ------------------------------------------------------------------ alarms

resource "aws_cloudwatch_metric_alarm" "executions_failed" {
  alarm_name          = "${var.name_prefix}-pipeline-executions-failed"
  alarm_description   = "A pipeline execution failed (validation or curated step). The execution itself already published details to the alerts topic."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsFailed"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  ok_actions          = [var.alerts_topic_arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.pipeline.arn
  }
}

resource "aws_cloudwatch_metric_alarm" "executions_timed_out" {
  alarm_name          = "${var.name_prefix}-pipeline-executions-timed-out"
  alarm_description   = "A pipeline execution hit the state machine's TimeoutSeconds."
  namespace           = "AWS/States"
  metric_name         = "ExecutionsTimedOut"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  ok_actions          = [var.alerts_topic_arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.pipeline.arn
  }
}

# The freshness/latency objective: a batch's curated tables exist within
# max_execution_seconds of the upload. ExecutionTime is a free AWS metric.
resource "aws_cloudwatch_metric_alarm" "execution_time" {
  alarm_name          = "${var.name_prefix}-pipeline-execution-time"
  alarm_description   = "A pipeline execution took longer than ${var.max_execution_seconds} s from upload to curated tables."
  namespace           = "AWS/States"
  metric_name         = "ExecutionTime"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = var.max_execution_seconds * 1000
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [var.alerts_topic_arn]
  ok_actions          = [var.alerts_topic_arn]

  dimensions = {
    StateMachineArn = aws_sfn_state_machine.pipeline.arn
  }
}
