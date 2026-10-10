terraform {
  required_version = ">= 1.9.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }

  # No backend block on purpose: this example is validated offline
  # (`terraform init -backend=false`) and is never planned or applied.
}
