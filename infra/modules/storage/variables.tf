variable "name_prefix" {
  description = "Prefix for every resource name, e.g. people-enrichment-dev."
  type        = string
}

variable "account_id" {
  description = "Account id appended to bucket names to keep them globally unique."
  type        = string
}

variable "force_destroy" {
  description = "Allow destroy to delete non-empty buckets (dev only)."
  type        = bool
  default     = false
}

variable "landing_retention_days" {
  type    = number
  default = 30
}

variable "raw_retention_days" {
  type    = number
  default = 90
}

variable "athena_results_retention_days" {
  type    = number
  default = 7
}

variable "table_read_capacity" {
  type    = number
  default = 5
}

variable "table_write_capacity" {
  type    = number
  default = 5
}
