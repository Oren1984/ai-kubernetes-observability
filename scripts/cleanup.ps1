<#
.SYNOPSIS
  Removes the project's local resources: the `aiobs` kind cluster, the Compose
  containers and their named volumes, and .runtime/. Source code, docs and
  test artifacts are kept. Never runs a Docker-wide prune.
.PARAMETER Force
  Required. Without it the script only lists what it would remove.
.PARAMETER IncludeAuditData
  Also delete data/ (the SQLite incident and audit history).
#>
param([switch]$Force, [switch]$IncludeAuditData)
. "$PSScriptRoot\common.ps1"
Set-Location $RepoRoot

if (-not $Force) {
    Write-Host 'Dry run. Re-run with -Force to remove:'
    Write-Host "  - kind cluster '$ClusterName' (container $NodeName)"
    Write-Host '  - Compose project aiobs: its containers and the volumes prometheus-data, loki-data, tempo-data, grafana-data'
    Write-Host '  - .runtime\ (kubeconfig, read-only token)'
    if ($IncludeAuditData) { Write-Host '  - data\ (SQLite incident and audit history)' }
    return
}

Write-Step 'Removing Compose containers and project volumes'
docker compose --profile test down --volumes --remove-orphans

Write-Step "Deleting kind cluster '$ClusterName'"
# A kind cluster is its node container; removing that container by exact name
# deletes the cluster without needing the kind binary.
if (docker ps -aq --filter "name=^$NodeName$") { docker rm --force --volumes $NodeName | Out-Null }

Remove-Item -Recurse -Force $RuntimeDir -ErrorAction SilentlyContinue
if ($IncludeAuditData) { Remove-Item -Recurse -Force "$RepoRoot\data" -ErrorAction SilentlyContinue }
Write-Host "`nProject resources removed. The shared 'kind' Docker network is left in place." -ForegroundColor Green
