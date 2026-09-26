output "database_name" {
  value = aws_glue_catalog_database.this.name
}

output "table_names" {
  value = sort(keys(aws_glue_catalog_table.this))
}

output "workgroup_name" {
  value = aws_athena_workgroup.this.name
}

output "named_query_ids" {
  value = { for k, q in aws_athena_named_query.question : k => q.id }
}
