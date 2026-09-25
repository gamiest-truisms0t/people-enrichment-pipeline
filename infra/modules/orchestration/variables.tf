variable "name_prefix" {
  description = "Prefix for every resource name, e.g. people-enrichment-dev."
  type        = string
}

variable "function_arns" {
  description = "ARNs of the three pipeline functions, keyed validate_input, enrich, build_curated."
  type        = map(string)

  validation {
    condition     = alltrue([for k in ["validate_input", "enrich", "build_curated"] : contains(keys(var.function_arns), k)])
    error_message = "function_arns must have the keys validate_input, enrich and build_curated."
  }
}

variable "landing_bucket_name" {
  description = "Bucket whose incoming/*.csv uploads start the pipeline."
  type        = string
}

variable "alerts_topic_arn" {
  description = "SNS topic that receives failure and partial-failure notifications."
  type        = string
}

variable "dead_letter_queue_arn" {
  description = "SQS queue that receives events EventBridge could not deliver to the state machine."
  type        = string
}

variable "max_concurrency" {
  description = "Parallel enrich invocations per batch. 1 keeps name-only lookups under the provider's 10/minute identify limit."
  type        = number
  default     = 1
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "log_execution_data" {
  description = "Include state input/output in the execution log. Off by default: rows contain names and emails."
  type        = bool
  default     = false
}
