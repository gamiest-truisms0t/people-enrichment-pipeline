locals {
  name_prefix = "${var.project}-${var.environment}"

  tags = merge(
    {
      project     = var.project
      environment = var.environment
      managed_by  = "terraform"
      stack       = "envs/${var.environment}"
    },
    var.owner == "" ? {} : { owner = var.owner },
  )
}
