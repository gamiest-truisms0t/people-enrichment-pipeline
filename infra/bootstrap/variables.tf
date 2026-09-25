variable "project" {
  description = "Project slug used in resource names and tags."
  type        = string
  default     = "people-enrichment"
}

variable "region" {
  description = "AWS region for the state bucket."
  type        = string
  default     = "ap-southeast-1"
}
