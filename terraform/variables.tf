variable "aws_region" {
  description = "AWS region the example targets."
  type        = string
  default     = "eu-central-1"
}

variable "project" {
  description = "Name used as a prefix and as the Project tag."
  type        = string
  default     = "aiobs"
}

variable "environment" {
  description = "Environment tag (for example: sandbox, staging)."
  type        = string
  default     = "sandbox"
}

variable "vpc_cidr" {
  description = "CIDR block of the VPC."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block."
  }
}

variable "instance_type" {
  description = "Instance type of the single Kubernetes node. Sized for the demo workload plus the telemetry stack."
  type        = string
  default     = "t3.large"
}

variable "telemetry_retention_days" {
  description = "Days of logs and traces kept in the telemetry bucket. Retention is the main cost lever for telemetry storage."
  type        = number
  default     = 14

  validation {
    condition     = var.telemetry_retention_days >= 1 && var.telemetry_retention_days <= 365
    error_message = "telemetry_retention_days must be between 1 and 365."
  }
}
