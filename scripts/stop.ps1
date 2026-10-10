<#
.SYNOPSIS
  Stops the project's containers without deleting anything. start.ps1 resumes them.
#>
. "$PSScriptRoot\common.ps1"
Set-Location $RepoRoot

Write-Step 'Stopping Compose services (data volumes are kept)'
docker compose stop

Write-Step "Stopping the kind node '$NodeName'"
if (docker ps -q --filter "name=^$NodeName$") { docker stop $NodeName | Out-Null }

Write-Host "`nStopped. Nothing was deleted; run .\scripts\start.ps1 to resume." -ForegroundColor Green
