# A single Lambda function with everything it needs and nothing shared:
# its own role (least privilege = base logging/metrics/tracing + the policy the
# caller passes in), an explicit log group with retention, X-Ray tracing, an
# optional dead-letter queue and an error alarm.

terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  ec2_arn_prefix = "arn:aws:ec2:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}"
  # Network interfaces Lambda may create for an optional VPC attachment: any ENI in the
  # account, but only in the configured subnets and security groups.
  vpc_network_arns = var.vpc_config == null ? [] : concat(
    ["${local.ec2_arn_prefix}:network-interface/*"],
    [for id in var.vpc_config.subnet_ids : "${local.ec2_arn_prefix}:subnet/${id}"],
    [for id in var.vpc_config.security_group_ids : "${local.ec2_arn_prefix}:security-group/${id}"],
  )
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "this" {
  name               = var.function_name
  assume_role_policy = data.aws_iam_policy_document.assume_role.json
}

data "aws_iam_policy_document" "base" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.this.arn}:*"]
  }

  # X-Ray and CloudWatch metrics do not support resource-level permissions.
  statement {
    sid       = "Tracing"
    actions   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords"]
    resources = ["*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [var.metrics_namespace]
    }
  }

  dynamic "statement" {
    for_each = var.dead_letter_target_arn == null ? [] : [var.dead_letter_target_arn]

    content {
      sid       = "DeadLetterQueue"
      actions   = ["sqs:SendMessage"]
      resources = [statement.value]
    }
  }

  # Only with a VPC attachment: the ENI lifecycle Lambda manages on the function's behalf.
  dynamic "statement" {
    for_each = var.vpc_config == null ? [] : [1]

    content {
      sid = "VpcNetworkInterfaces"
      actions = [
        "ec2:CreateNetworkInterface",
        "ec2:DeleteNetworkInterface",
        "ec2:AssignPrivateIpAddresses",
        "ec2:UnassignPrivateIpAddresses",
      ]
      resources = local.vpc_network_arns
    }
  }

  dynamic "statement" {
    for_each = var.vpc_config == null ? [] : [1]

    content {
      # Describe calls do not support resource-level permissions.
      sid       = "VpcDescribe"
      actions   = ["ec2:DescribeNetworkInterfaces", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"]
      resources = ["*"]
    }
  }
}

resource "aws_iam_role_policy" "base" {
  name   = "base"
  role   = aws_iam_role.this.id
  policy = data.aws_iam_policy_document.base.json
}

resource "aws_iam_role_policy" "extra" {
  name   = "function"
  role   = aws_iam_role.this.id
  policy = var.policy_json
}

resource "aws_lambda_function" "this" {
  function_name    = var.function_name
  description      = var.description
  role             = aws_iam_role.this.arn
  handler          = var.handler
  runtime          = var.runtime
  architectures    = [var.architecture]
  filename         = var.filename
  source_code_hash = var.source_code_hash
  memory_size      = var.memory_size
  timeout          = var.timeout
  layers           = var.layers

  environment {
    variables = var.environment
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.this.name
  }

  tracing_config {
    mode = var.tracing_mode
  }

  dynamic "dead_letter_config" {
    for_each = var.dead_letter_target_arn == null ? [] : [var.dead_letter_target_arn]

    content {
      target_arn = dead_letter_config.value
    }
  }

  dynamic "vpc_config" {
    for_each = var.vpc_config == null ? [] : [var.vpc_config]

    content {
      subnet_ids         = vpc_config.value.subnet_ids
      security_group_ids = vpc_config.value.security_group_ids
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.this,
    aws_iam_role_policy.base,
    aws_iam_role_policy.extra,
  ]
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  alarm_name          = "${var.function_name}-errors"
  alarm_description   = "Invocation errors for ${var.function_name}"
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions

  dimensions = {
    FunctionName = aws_lambda_function.this.function_name
  }
}
