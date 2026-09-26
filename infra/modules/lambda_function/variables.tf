variable "function_name" {
  type = string
}

variable "description" {
  type    = string
  default = ""
}

variable "handler" {
  description = "Python entry point, e.g. enrich_pipeline.handlers.enrich.handler."
  type        = string
}

variable "filename" {
  description = "Path to the deployment zip."
  type        = string
}

variable "source_code_hash" {
  description = "Base64 SHA-256 of the zip; changing it redeploys the function."
  type        = string
}

variable "runtime" {
  type    = string
  default = "python3.13"
}

variable "architecture" {
  type    = string
  default = "arm64"

  validation {
    condition     = contains(["arm64", "x86_64"], var.architecture)
    error_message = "architecture must be arm64 or x86_64."
  }
}

variable "memory_size" {
  type    = number
  default = 256
}

variable "timeout" {
  type    = number
  default = 30
}

variable "environment" {
  type    = map(string)
  default = {}
}

variable "layers" {
  type    = list(string)
  default = []
}

variable "policy_json" {
  description = "Function-specific IAM policy document (JSON). Required: every function gets least-privilege access to exactly what it touches."
  type        = string
}

variable "log_retention_days" {
  type    = number
  default = 14
}

variable "dead_letter_target_arn" {
  description = "SQS queue (or SNS topic) ARN for failed asynchronous invocations."
  type        = string
  default     = null
}

variable "alarm_actions" {
  type    = list(string)
  default = []
}

variable "tracing_mode" {
  type    = string
  default = "Active"
}

variable "metrics_namespace" {
  description = "CloudWatch namespace the function may publish custom metrics to."
  type        = string
  default     = "PeopleEnrichment"
}

variable "vpc_config" {
  description = <<-EOT
    Attach the function to existing private subnets (PLAN.md D11). null, the default, keeps
    the function outside any VPC: it only makes outbound HTTPS calls, and inside a VPC those
    would need a NAT gateway (not free) or interface endpoints. When set, the role also gets
    the network-interface permissions Lambda needs, scoped to the given subnets and groups.
  EOT
  type = object({
    subnet_ids         = list(string)
    security_group_ids = list(string)
  })
  default = null
}
