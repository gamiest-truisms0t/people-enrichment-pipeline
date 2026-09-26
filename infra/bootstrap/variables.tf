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

variable "github_repository" {
  description = "GitHub repository (owner/name) whose Actions workflows may assume the CI roles."
  type        = string
  default     = "gamiest-truisms0t/people-enrichment-pipeline"
}

variable "environments" {
  description = "Environment stacks whose state the CI roles may read (state key <env>/terraform.tfstate)."
  type        = list(string)
  default     = ["dev"]
}
