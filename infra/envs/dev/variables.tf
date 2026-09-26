variable "project" {
  description = "Project slug used in resource names and tags."
  type        = string
  default     = "people-enrichment"
}

variable "environment" {
  description = "Environment name; also the state key prefix."
  type        = string
  default     = "dev"
}

variable "owner" {
  description = "Owner tag on every resource (a team or handle). Empty = tag omitted."
  type        = string
  default     = ""
}

variable "region" {
  type    = string
  default = "ap-southeast-1"
}

variable "provider_name" {
  description = "Enrichment provider the enrich function uses: mock (Phases 2-3) or pdl (Phase 4)."
  type        = string
  default     = "mock"

  validation {
    condition     = contains(["mock", "pdl"], var.provider_name)
    error_message = "provider_name must be mock or pdl."
  }
}

variable "pdl_sandbox" {
  description = "Point the pdl provider at the free synthetic sandbox host instead of production."
  type        = bool
  default     = false
}

variable "max_rows" {
  description = "Row cap per input file (keeps the Step Functions payload small)."
  type        = number
  default     = 500
}

# Data guards (batch-level thresholds; field rules live in src/enrich_pipeline/guards.py).
variable "max_input_bytes" {
  description = "Uploaded CSVs above this size fail validation before being read."
  type        = number
  default     = 5000000
}

variable "max_invalid_fraction" {
  description = "Share of rejected rows above which the file is treated as the wrong layout and the batch fails (files of 5+ rows)."
  type        = number
  default     = 0.5

  validation {
    condition     = var.max_invalid_fraction > 0 && var.max_invalid_fraction <= 1
    error_message = "max_invalid_fraction must be in (0, 1]."
  }
}

variable "min_match_rate" {
  description = "Share of valid rows matched (or cached) below which the batch completes with a data-quality warning."
  type        = number
  default     = 0.2

  validation {
    condition     = var.min_match_rate >= 0 && var.min_match_rate <= 1
    error_message = "min_match_rate must be in [0, 1]."
  }
}

variable "max_enrich_credits" {
  description = "Monthly ceiling for enrich credits (billed only on a match). Below the free 100."
  type        = number
  default     = 70
}

variable "max_identify_credits" {
  description = "Monthly ceiling for identify credits (billed on every call). The free plan grants only 5 per month; this keeps a reserve."
  type        = number
  default     = 2
}

variable "identify_min_score" {
  type    = number
  default = 70
}

variable "identify_min_margin" {
  type    = number
  default = 20
}

variable "enrich_min_likelihood" {
  description = "Provider-side match threshold for enrich calls (1-10). PDL scores correct name+company matches of well-known people around 4; 6 discarded most true matches."
  type        = number
  default     = 4
}

variable "location_hint" {
  description = "Optional event location appended to name-only lookups (e.g. \"Singapore\")."
  type        = string
  default     = ""
}

variable "budget_alarm_fraction" {
  description = "Fraction of a monthly credit ceiling at which the budget alarms fire."
  type        = number
  default     = 0.9

  validation {
    condition     = var.budget_alarm_fraction > 0 && var.budget_alarm_fraction <= 1
    error_message = "budget_alarm_fraction must be in (0, 1]."
  }
}

variable "projection_start_date" {
  description = "First batch_date Athena partition projection covers (yyyy-MM-dd)."
  type        = string
  default     = "2026-09-01"
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "raw_retention_days" {
  description = "Lifecycle expiry for raw/ objects in the data bucket (dev only)."
  type        = number
  default     = 90
}

variable "force_destroy_buckets" {
  description = "Allow terraform destroy to empty the buckets. Keep true in dev, false anywhere real."
  type        = bool
  default     = true
}

variable "lambda_package_dir" {
  description = "Directory produced by `make package`, relative to this stack."
  type        = string
  default     = "../../../build/lambda"
}

variable "pandas_layer_arn" {
  description = "AWS-managed AWS SDK for pandas layer (pyarrow) for the build-curated function."
  type        = string
  default     = "arn:aws:lambda:ap-southeast-1:336392948345:layer:AWSSDKPandas-Python313-Arm64:16"
}

variable "alert_email" {
  description = "Email address subscribed to the alerts topic and to the AWS Budget. Confirm the subscription email once after apply."
  type        = string
}

variable "monthly_budget_usd" {
  description = "AWS Budgets monthly cost limit for the account; alerts at 20 % actual and 100 % forecast."
  type        = number
  default     = 5
}

variable "lambda_vpc_config" {
  description = <<-EOT
    Optional VPC attachment for the three functions (PLAN.md D11): existing private subnet
    ids and security group ids. Leave null unless the subnets have a NAT gateway or
    interface endpoints for the provider API, S3, DynamoDB, SSM, CloudWatch and X-Ray.
  EOT
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}

variable "max_concurrency" {
  description = "Parallel enrich invocations per batch. 1 keeps name-only lookups under the provider's 10/minute identify limit."
  type        = number
  default     = 1
}
