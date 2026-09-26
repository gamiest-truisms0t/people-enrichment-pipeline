# Analytics layer: a Glue database with one external table per curated Parquet table,
# partition projection over batch_date (no crawler, no MSCK REPAIR), and an Athena
# workgroup with encrypted results and a per-query scan cutoff. Column definitions come
# from columns.json, generated from src/enrich_pipeline/schema.py by `make glue-columns`.

terraform {
  required_version = ">= 1.16"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0"
    }
  }
}

locals {
  catalog          = jsondecode(file("${path.module}/columns.json"))
  partition_column = local.catalog.partition_column
  tables           = local.catalog.tables
  database_name    = "${local.catalog.database_base_name}_${var.environment}"
  curated_prefix   = "s3://${var.data_bucket_name}/curated"
}

resource "aws_glue_catalog_database" "this" {
  name        = local.database_name
  description = "Curated people-enrichment tables (${var.environment}); one Parquet file per table per batch"
}

resource "aws_glue_catalog_table" "this" {
  for_each = local.tables

  name          = each.key
  database_name = aws_glue_catalog_database.this.name
  table_type    = "EXTERNAL_TABLE"
  description   = "Curated ${each.key}; partitioned by ${local.partition_column} (projected)"

  parameters = {
    "classification"      = "parquet"
    "EXTERNAL"            = "TRUE"
    "parquet.compression" = "SNAPPY"

    # Partition projection: Athena derives partitions from the date range instead of
    # the catalog, so new batches are queryable the moment their file lands.
    "projection.enabled"                                 = "true"
    "projection.${local.partition_column}.type"          = "date"
    "projection.${local.partition_column}.format"        = "yyyy-MM-dd"
    "projection.${local.partition_column}.range"         = "${var.projection_start_date},NOW"
    "projection.${local.partition_column}.interval"      = "1"
    "projection.${local.partition_column}.interval.unit" = "DAYS"
    "storage.location.template"                          = "${local.curated_prefix}/${each.key}/${local.partition_column}=$${${local.partition_column}}/"
  }

  storage_descriptor {
    location      = "${local.curated_prefix}/${each.key}/"
    input_format  = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"
    output_format = "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"

    ser_de_info {
      name                  = "parquet"
      serialization_library = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
      parameters = {
        "serialization.format" = "1"
      }
    }

    dynamic "columns" {
      for_each = each.value

      content {
        name    = columns.value.name
        type    = columns.value.type
        comment = columns.value.comment
      }
    }
  }

  partition_keys {
    name = local.partition_column
    type = "string"
  }
}

resource "aws_athena_workgroup" "this" {
  name        = "${var.name_prefix}-analytics"
  description = "Queries over the curated people-enrichment tables"
  state       = "ENABLED"

  configuration {
    enforce_workgroup_configuration    = true
    publish_cloudwatch_metrics_enabled = true
    bytes_scanned_cutoff_per_query     = var.bytes_scanned_cutoff_per_query

    engine_version {
      selected_engine_version = "Athena engine version 3"
    }

    result_configuration {
      output_location = "s3://${var.data_bucket_name}/athena-results/"

      encryption_configuration {
        encryption_option = "SSE_S3"
      }
    }
  }

  # Dev workgroup: results are transient (lifecycle expires athena-results/ after 7 days).
  force_destroy = true
}

# The brief's three questions, saved in the workgroup so they show up in the console.
resource "aws_athena_named_query" "question" {
  for_each = {
    "1-who-are-the-individuals" = "who.sql.tftpl"
    "2-which-companies"         = "companies.sql.tftpl"
    "3-which-roles"             = "roles.sql.tftpl"
    "4-outcome-per-input-row"   = "outcomes.sql.tftpl"
  }

  name        = each.key
  description = "People-enrichment: ${replace(each.key, "-", " ")}"
  database    = aws_glue_catalog_database.this.name
  workgroup   = aws_athena_workgroup.this.id
  query       = templatefile("${path.module}/queries/${each.value}", { database = aws_glue_catalog_database.this.name })
}
