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

variable "github_owner_id" {
  description = "Numeric id of the repository owner, part of GitHub's immutable OIDC subject (gh api users/<owner> --jq .id)."
  type        = number
  default     = 333739224
}

variable "github_repository_id" {
  description = "Numeric id of the repository, part of GitHub's immutable OIDC subject (gh api repos/<owner>/<name> --jq .id)."
  type        = number
  default     = 1387328218
}
