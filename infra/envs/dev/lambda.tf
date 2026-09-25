# One deployment package (built by `make package`) serves all three functions;
# each points at a different handler. source_code_hash drives redeploys.

data "archive_file" "lambda" {
  type        = "zip"
  source_dir  = "${path.module}/${var.lambda_package_dir}"
  output_path = "${path.module}/${var.lambda_package_dir}.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

locals {
  common_environment = {
    DATA_BUCKET                  = module.storage.data_bucket_name
    LANDING_BUCKET               = module.storage.landing_bucket_name
    STATE_TABLE                  = module.storage.state_table_name
    PROVIDER                     = var.provider_name
    PDL_API_KEY_PARAM            = module.secrets.pdl_api_key_parameter_name
    MAX_ROWS                     = tostring(var.max_rows)
    MAX_ENRICH_CREDITS           = tostring(var.max_enrich_credits)
    MAX_IDENTIFY_CREDITS         = tostring(var.max_identify_credits)
    IDENTIFY_MIN_SCORE           = tostring(var.identify_min_score)
    IDENTIFY_MIN_MARGIN          = tostring(var.identify_min_margin)
    ENRICH_MIN_LIKELIHOOD        = tostring(var.enrich_min_likelihood)
    LOCATION_HINT                = var.location_hint
    POWERTOOLS_METRICS_NAMESPACE = "PeopleEnrichment"
    POWERTOOLS_LOG_LEVEL         = "INFO"
  }
}

# ------------------------------------------------------------------ IAM per function

data "aws_iam_policy_document" "validate_input" {
  statement {
    sid       = "ReadLanding"
    actions   = ["s3:GetObject"]
    resources = ["${module.storage.landing_bucket_arn}/incoming/*"]
  }

  statement {
    sid       = "WriteParsedInput"
    actions   = ["s3:PutObject"]
    resources = ["${module.storage.data_bucket_arn}/input/*"]
  }
}

data "aws_iam_policy_document" "enrich" {
  statement {
    sid       = "StateTable"
    actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
    resources = [module.storage.state_table_arn]
  }

  statement {
    sid     = "WriteRawAndResults"
    actions = ["s3:PutObject"]
    resources = [
      "${module.storage.data_bucket_arn}/raw/*",
      "${module.storage.data_bucket_arn}/results/*",
    ]
  }

  statement {
    sid       = "ReadApiKey"
    actions   = ["ssm:GetParameter"]
    resources = [module.secrets.pdl_api_key_parameter_arn]
  }

  statement {
    sid       = "DecryptApiKey"
    actions   = ["kms:Decrypt"]
    resources = [data.aws_kms_alias.ssm.target_key_arn]
  }
}

data "aws_iam_policy_document" "build_curated" {
  statement {
    sid       = "ListBatchObjects"
    actions   = ["s3:ListBucket"]
    resources = [module.storage.data_bucket_arn]

    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["results/*", "input/*"]
    }
  }

  statement {
    sid     = "ReadBatchObjects"
    actions = ["s3:GetObject"]
    resources = [
      "${module.storage.data_bucket_arn}/results/*",
      "${module.storage.data_bucket_arn}/input/*",
    ]
  }

  statement {
    sid     = "WriteCurated"
    actions = ["s3:PutObject"]
    resources = [
      "${module.storage.data_bucket_arn}/curated/*",
      "${module.storage.data_bucket_arn}/manifests/*",
    ]
  }
}

# ------------------------------------------------------------------ functions

module "fn_validate_input" {
  source = "../../modules/lambda_function"

  function_name          = "${local.name_prefix}-validate-input"
  description            = "Parse and validate an uploaded registrant CSV"
  handler                = "enrich_pipeline.handlers.validate_input.handler"
  filename               = data.archive_file.lambda.output_path
  source_code_hash       = data.archive_file.lambda.output_base64sha256
  memory_size            = 512
  timeout                = 60
  environment            = merge(local.common_environment, { POWERTOOLS_SERVICE_NAME = "validate-input" })
  policy_json            = data.aws_iam_policy_document.validate_input.json
  log_retention_days     = var.log_retention_days
  dead_letter_target_arn = aws_sqs_queue.lambda_dlq.arn
  alarm_actions          = [aws_sns_topic.alerts.arn]
}

module "fn_enrich" {
  source = "../../modules/lambda_function"

  function_name          = "${local.name_prefix}-enrich"
  description            = "Enrich one registrant via the configured provider"
  handler                = "enrich_pipeline.handlers.enrich.handler"
  filename               = data.archive_file.lambda.output_path
  source_code_hash       = data.archive_file.lambda.output_base64sha256
  memory_size            = 512
  timeout                = 90
  environment            = merge(local.common_environment, { POWERTOOLS_SERVICE_NAME = "enrich" })
  policy_json            = data.aws_iam_policy_document.enrich.json
  log_retention_days     = var.log_retention_days
  dead_letter_target_arn = aws_sqs_queue.lambda_dlq.arn
  alarm_actions          = [aws_sns_topic.alerts.arn]
}

module "fn_build_curated" {
  source = "../../modules/lambda_function"

  function_name          = "${local.name_prefix}-build-curated"
  description            = "Build the curated Parquet tables for a batch"
  handler                = "enrich_pipeline.handlers.build_curated.handler"
  filename               = data.archive_file.lambda.output_path
  source_code_hash       = data.archive_file.lambda.output_base64sha256
  memory_size            = 1024
  timeout                = 300
  layers                 = [var.pandas_layer_arn]
  environment            = merge(local.common_environment, { POWERTOOLS_SERVICE_NAME = "build-curated" })
  policy_json            = data.aws_iam_policy_document.build_curated.json
  log_retention_days     = var.log_retention_days
  dead_letter_target_arn = aws_sqs_queue.lambda_dlq.arn
  alarm_actions          = [aws_sns_topic.alerts.arn]
}
