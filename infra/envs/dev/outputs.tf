output "landing_bucket" {
  value = module.storage.landing_bucket_name
}

output "data_bucket" {
  value = module.storage.data_bucket_name
}

output "state_table" {
  value = module.storage.state_table_name
}

output "pdl_api_key_parameter" {
  value = module.secrets.pdl_api_key_parameter_name
}

output "lambda_dlq_url" {
  value = aws_sqs_queue.lambda_dlq.url
}

output "state_machine_arn" {
  value = module.orchestration.state_machine_arn
}

output "state_machine_name" {
  value = module.orchestration.state_machine_name
}

output "alerts_topic_arn" {
  value = aws_sns_topic.alerts.arn
}

output "event_rule_name" {
  value = module.orchestration.event_rule_name
}

output "glue_database" {
  value = module.catalog.database_name
}

output "glue_tables" {
  value = module.catalog.table_names
}

output "athena_workgroup" {
  value = module.catalog.workgroup_name
}

output "function_names" {
  value = {
    validate_input = module.fn_validate_input.function_name
    enrich         = module.fn_enrich.function_name
    build_curated  = module.fn_build_curated.function_name
  }
}

output "function_arns" {
  value = {
    validate_input = module.fn_validate_input.function_arn
    enrich         = module.fn_enrich.function_arn
    build_curated  = module.fn_build_curated.function_arn
  }
}
