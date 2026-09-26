variable "name_prefix" {
  description = "Prefix for resource names, e.g. people-enrichment-dev."
  type        = string
}

variable "environment" {
  description = "Suffix for the Glue database name."
  type        = string
}

variable "data_bucket_name" {
  description = "Bucket holding curated/<table>/batch_date=YYYY-MM-DD/*.parquet and athena-results/."
  type        = string
}

variable "projection_start_date" {
  description = "First batch_date Athena should project (yyyy-MM-dd); the range ends at NOW."
  type        = string
  default     = "2026-09-01"
}

variable "bytes_scanned_cutoff_per_query" {
  description = "Athena aborts a query that would scan more than this many bytes (min 10 MB)."
  type        = number
  default     = 104857600 # 100 MB
}
