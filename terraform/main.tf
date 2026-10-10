# Illustrative AWS footprint for this POC: one private Kubernetes node (k3s or
# kind on EC2) and an S3 bucket as the object store for Loki and Tempo.
#
# This is an example that is validated offline only. It has never been planned
# or applied, and it is not a production design. See docs/TERRAFORM_VALIDATION.md.

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = var.project
      Environment = var.environment
      ManagedBy   = "terraform"
      Repository  = "ai-kubernetes-observability"
    }
  }
}

data "aws_availability_zones" "available" {
  state = "available"
}

data "aws_ssm_parameter" "al2023_ami" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
}

locals {
  name = "${var.project}-${var.environment}"
}

# --- network -----------------------------------------------------------------

resource "aws_vpc" "this" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.name }
}

resource "aws_subnet" "private" {
  vpc_id                  = aws_vpc.this.id
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 1)
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = false

  tags = { Name = "${local.name}-private" }
}

# No inbound rules: the node is reached through SSM Session Manager, the same
# "local-only exposure" stance the POC takes on the workstation.
resource "aws_security_group" "node" {
  name_prefix = "${local.name}-node-"
  description = "Kubernetes node for the observability POC"
  vpc_id      = aws_vpc.this.id

  tags = { Name = "${local.name}-node" }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_vpc_security_group_egress_rule" "https_out" {
  security_group_id = aws_security_group.node.id
  description       = "HTTPS egress for image pulls, SSM and S3"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

# --- telemetry storage --------------------------------------------------------

resource "aws_s3_bucket" "telemetry" {
  bucket_prefix = "${local.name}-telemetry-"

  tags = { Name = "${local.name}-telemetry", Purpose = "loki-and-tempo-object-store" }
}

resource "aws_s3_bucket_public_access_block" "telemetry" {
  bucket                  = aws_s3_bucket.telemetry.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "telemetry" {
  bucket = aws_s3_bucket.telemetry.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "telemetry" {
  bucket = aws_s3_bucket.telemetry.id

  rule {
    id     = "expire-telemetry"
    status = "Enabled"

    filter {}

    expiration {
      days = var.telemetry_retention_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 2
    }
  }
}

# --- node identity: least privilege ------------------------------------------

data "aws_iam_policy_document" "assume_ec2" {
  statement {
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "telemetry_bucket" {
  statement {
    sid       = "ListTelemetryBucket"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.telemetry.arn]
  }

  statement {
    sid       = "ReadWriteTelemetryObjects"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.telemetry.arn}/*"]
  }
}

resource "aws_iam_role" "node" {
  name_prefix        = "${local.name}-node-"
  assume_role_policy = data.aws_iam_policy_document.assume_ec2.json
}

resource "aws_iam_role_policy" "telemetry_bucket" {
  name_prefix = "telemetry-bucket-"
  role        = aws_iam_role.node.id
  policy      = data.aws_iam_policy_document.telemetry_bucket.json
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.node.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "node" {
  name_prefix = "${local.name}-node-"
  role        = aws_iam_role.node.name
}

# --- compute ------------------------------------------------------------------

resource "aws_instance" "node" {
  ami                    = data.aws_ssm_parameter.al2023_ami.value
  instance_type          = var.instance_type
  subnet_id              = aws_subnet.private.id
  vpc_security_group_ids = [aws_security_group.node.id]
  iam_instance_profile   = aws_iam_instance_profile.node.name

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type = "gp3"
    volume_size = 40
    encrypted   = true
  }

  tags = { Name = "${local.name}-k8s-node" }
}
