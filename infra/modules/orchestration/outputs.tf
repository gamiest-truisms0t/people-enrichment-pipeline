output "state_machine_arn" {
  value = aws_sfn_state_machine.pipeline.arn
}

output "state_machine_name" {
  value = aws_sfn_state_machine.pipeline.name
}

output "event_rule_arn" {
  value = aws_cloudwatch_event_rule.csv_uploaded.arn
}

output "event_rule_name" {
  value = aws_cloudwatch_event_rule.csv_uploaded.name
}

output "log_group_name" {
  value = aws_cloudwatch_log_group.state_machine.name
}
