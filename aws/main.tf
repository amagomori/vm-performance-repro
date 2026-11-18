terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {}

resource "aws_s3_bucket" "snapshots" {
  bucket_prefix = "vmrepro-snapshots-"
  force_destroy = true
}

resource "aws_dynamodb_table" "hosts" {
  name         = "vmrepro-hosts"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "host_id"

  attribute {
    name = "host_id"
    type = "S"
  }
}

resource "aws_sqs_queue" "pool_events" {
  name = "vmrepro-pool-events"
}

resource "aws_cloudwatch_event_rule" "task_state" {
  name = "vmrepro-task-state"
  event_pattern = jsonencode({
    source        = ["vmrepro.test"]
    "detail-type" = ["Task State Change"]
    detail        = { lastStatus = ["RUNNING", "STOPPED"] }
  })
}

resource "aws_cloudwatch_event_target" "to_queue" {
  rule = aws_cloudwatch_event_rule.task_state.name
  arn  = aws_sqs_queue.pool_events.arn
}

resource "aws_sqs_queue_policy" "allow_events" {
  queue_url = aws_sqs_queue.pool_events.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "events.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.pool_events.arn
      Condition = { ArnEquals = { "aws:SourceArn" = aws_cloudwatch_event_rule.task_state.arn } }
    }]
  })
}

output "bucket" {
  value = aws_s3_bucket.snapshots.bucket
}

output "table" {
  value = aws_dynamodb_table.hosts.name
}

output "queue_url" {
  value = aws_sqs_queue.pool_events.url
}
