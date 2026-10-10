# Terraform validation

`terraform/` is a small, illustrative example of where this POC could run on AWS. It was validated offline. **Nothing was planned, applied or created, and no AWS credentials were used.**

## What the example describes

- A VPC with one private subnet.
- One EC2 instance (`t3.large`, Amazon Linux 2023, IMDSv2 only, encrypted gp3 root volume) as a single Kubernetes node.
- A security group with no inbound rules and HTTPS-only egress; access is intended through SSM Session Manager.
- An S3 bucket for Loki and Tempo object storage: public access blocked, encrypted, with a lifecycle rule that expires telemetry after `telemetry_retention_days` (default 14).
- An IAM role for the node limited to that bucket plus the SSM managed policy.
- Default tags on every resource: `Project`, `Environment`, `ManagedBy`, `Repository`.

It demonstrates provider configuration, version constraints, variables with validation, data sources, tagging, least-privilege IAM and outputs.

## What was run

Only these three commands:

```bash
terraform fmt -check -recursive
terraform init -backend=false -input=false
terraform validate
```

| Command | On the Windows host (Terraform 1.15.8) | In `hashicorp/terraform:1.15.8` container |
|---|---|---|
| `fmt -check -recursive` | PASS | PASS |
| `init -backend=false -input=false` | PASS — installed `hashicorp/aws` v6.68.0 | PASS — installed `hashicorp/aws` v6.68.0 |
| `validate` | **FAILED to run** (see below) | PASS — "Success! The configuration is valid." |

### Why `validate` did not run on the host

Terraform talks to its provider plugin over a local TLS connection. On this workstation the antivirus's HTTPS inspection (Norton) intercepts that connection and re-signs it, so Terraform rejects the plugin:

```
plugin.(*GRPCProvider).GetProviderSchema: ... tls: failed to verify certificate: x509: certificate signed by unknown authority
```

This is an environment issue, not a configuration error; it failed the same way on five attempts. The same three commands were therefore run in the official Terraform container against a copy of the same files, where they passed. The antivirus configuration was not changed.

To reproduce the container run from the repository root:

```powershell
docker run --rm --entrypoint sh -v "${PWD}\terraform:/tf:ro" hashicorp/terraform:1.15.8 -c "mkdir /work && cp /tf/*.tf /tf/.terraform.lock.hcl /work/ && cd /work && terraform fmt -check -recursive && terraform init -backend=false -input=false && terraform validate"
```

### Provider initialisation

`terraform init` downloads the AWS provider (about 800 MB unpacked on Windows) from the public Terraform registry into `terraform/.terraform/`, which is git-ignored. This needs internet access but no AWS account or credentials. `-backend=false` means no state backend is configured or contacted. The container run confirmed that no `AWS_*` environment variables were present.

## What was not done

- `terraform plan`, `apply` and `destroy` were never run.
- No AWS resource exists because of this project, and no cost was incurred.
- `validate` checks syntax, types and references against the provider schema. It does not prove that the configuration would apply successfully: AMI availability, quotas, IAM permissions and naming collisions are only discovered by `plan`/`apply`.

## What production use would need

- **An egress path.** The private subnet has no NAT gateway or VPC endpoints, so the instance as written could not pull images or reach SSM and S3. That is omitted to keep the example small.
- A remote state backend with locking, and a second availability zone.
- A real Kubernetes platform decision (EKS with managed node groups, or a hardened k3s install) and its bootstrap; this example stops at the instance.
- Load balancing, DNS and TLS for Grafana and the dashboard, with authentication.
- KMS keys instead of S3-managed encryption, bucket versioning, and access logging.
- Managed or highly available telemetry backends instead of single-binary Loki, Tempo and Prometheus.
- Review by someone accountable for the AWS account's security baseline.
