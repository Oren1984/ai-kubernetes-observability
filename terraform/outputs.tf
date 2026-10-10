output "vpc_id" {
  description = "ID of the example VPC."
  value       = aws_vpc.this.id
}

output "node_instance_id" {
  description = "Instance ID of the Kubernetes node (connect with SSM Session Manager)."
  value       = aws_instance.node.id
}

output "telemetry_bucket" {
  description = "S3 bucket intended as the Loki and Tempo object store."
  value       = aws_s3_bucket.telemetry.bucket
}
