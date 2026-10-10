<#
.SYNOPSIS
  Brings the whole local POC up: kind cluster, workload, Collector, backends, dashboard.
  Safe to re-run; it only ever touches resources named `aiobs*`.
.PARAMETER SkipBuild
  Reuse the existing aiobs/demo-api:dev image instead of rebuilding it.
#>
param([switch]$SkipBuild)
. "$PSScriptRoot\common.ps1"
Set-Location $RepoRoot

Write-Step 'Checking prerequisites'
Assert-Command docker
docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker is not running. Start Docker Desktop first.' }
New-Item -ItemType Directory -Force $RuntimeDir, "$RuntimeDir\k8s", "$RepoRoot\data" | Out-Null

if (-not (Test-Path .env)) {
    $password = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 20 | ForEach-Object { [char]$_ })
    (Get-Content .env.example) -replace '^GRAFANA_ADMIN_PASSWORD=.*', "GRAFANA_ADMIN_PASSWORD=$password" | Set-Content .env
    Write-Host '  created .env with a generated Grafana admin password'
}

Write-Step "Kubernetes cluster '$ClusterName'"
# kind is only needed to create the cluster; an existing one is driven through Docker.
if (docker ps -aq --filter "name=^$NodeName$") {
    if ((docker inspect -f '{{.State.Running}}' $NodeName) -ne 'true') { docker start $NodeName | Out-Null }
} else {
    & (Get-Kind) create cluster --config kubernetes\kind-config.yaml --kubeconfig $Kubeconfig --wait 120s
    if ($LASTEXITCODE -ne 0) { throw 'kind create cluster failed.' }
}
Wait-Until { (Invoke-Kubectl get nodes --no-headers 2>$null) -match '\sReady\s' } 'node Ready'

Write-Step 'Application image'
$imageBefore = docker image inspect -f '{{.Id}}' $AppImage 2>$null
if (-not $SkipBuild) {
    docker build -q -t $AppImage app | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'docker build failed.' }
}
$imageChanged = (docker image inspect -f '{{.Id}}' $AppImage) -ne $imageBefore
# Same effect as `kind load docker-image`, without needing the kind binary.
Invoke-NodeShell 'rm -rf /aiobs && mkdir -p /aiobs'
docker save $AppImage -o "$RuntimeDir\demo-api.tar"
docker cp "$RuntimeDir\demo-api.tar" "${NodeName}:/aiobs/demo-api.tar" | Out-Null
Remove-Item "$RuntimeDir\demo-api.tar"
docker exec $NodeName ctr --namespace=k8s.io images import /aiobs/demo-api.tar | Out-Null
if ($LASTEXITCODE -ne 0) { throw 'Loading the image into the kind node failed.' }

Write-Step 'Deploying to Kubernetes'
# Manifests are copied into the node and applied from there (see Invoke-Kubectl).
docker cp kubernetes "${NodeName}:/aiobs/kubernetes" | Out-Null
docker cp observability\otel-collector.yaml "${NodeName}:/aiobs/otel-collector.yaml" | Out-Null
Invoke-Kubectl apply -f /aiobs/kubernetes/namespace.yaml
$collectorConfig = Invoke-NodeShell ('kubectl create configmap otel-collector-config -n aiobs-observability ' +
    '--from-file=otel-collector.yaml=/aiobs/otel-collector.yaml --dry-run=client -o yaml | kubectl apply -f -')
$collectorConfig
$manifests = 'otel-collector', 'configmap', 'evidence-reader-rbac', 'deployment', 'service' |
    ForEach-Object { '-f'; "/aiobs/kubernetes/$_.yaml" }
Invoke-Kubectl apply @manifests
if ($LASTEXITCODE -ne 0) { throw 'kubectl apply failed.' }
# Neither a ConfigMap edit nor a re-tagged :dev image restarts pods by itself.
if ($collectorConfig -match 'configured') { Invoke-Kubectl rollout restart deploy/otel-collector -n aiobs-observability }
if ($imageChanged -and $imageBefore) { Invoke-Kubectl rollout restart deploy/demo-api deploy/traffic-generator -n aiobs-demo }
foreach ($target in @('otel-collector', 'aiobs-observability'), @('demo-api', 'aiobs-demo'), @('traffic-generator', 'aiobs-demo')) {
    Invoke-Kubectl rollout status "deploy/$($target[0])" -n $target[1] --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        Invoke-Kubectl logs "deploy/$($target[0])" -n $target[1] --tail=20
        throw "Deployment $($target[0]) did not become ready (its last log lines are above)."
    }
}

Write-Step 'Read-only credentials for the incident engine'
# Short-lived token for the namespaced, read-only `evidence-reader` account.
$token = Invoke-Kubectl create token evidence-reader -n aiobs-demo --duration=24h
if ($LASTEXITCODE -ne 0) { throw 'Could not issue the evidence-reader token.' }
[IO.File]::WriteAllText("$RuntimeDir\k8s\token", "$token".Trim())
$ca = (docker exec $NodeName cat /etc/kubernetes/pki/ca.crt) -join "`n"
[IO.File]::WriteAllText("$RuntimeDir\k8s\ca.crt", "$ca`n")

Write-Step 'Telemetry backends, Grafana and dashboard (Docker Compose)'
docker compose build --quiet
docker compose up -d --quiet-pull
if ($LASTEXITCODE -ne 0) { throw 'docker compose up failed.' }

Write-Step 'Waiting for services'
Wait-Until { Test-Url 'http://localhost:19090/-/ready' } 'Prometheus'
Wait-Until { Test-Url 'http://localhost:13100/ready' } 'Loki'
Wait-Until { Test-Url 'http://localhost:13200/ready' } 'Tempo'
Wait-Until { Test-Url 'http://localhost:13000/api/health' } 'Grafana'
Wait-Until { Test-Url 'http://localhost:18080/ready' } 'demo-api via Kubernetes NodePort'
Wait-Until { Test-Url 'http://localhost:18501/_stcore/health' } 'Incident dashboard'

Write-Host "`nSystem is up." -ForegroundColor Green
$Urls.GetEnumerator() | ForEach-Object { '  {0,-32} {1}' -f $_.Key, $_.Value }
Write-Host "`nNext: .\scripts\verify.ps1   (the K8s read token is valid for 24h; re-run start.ps1 to renew)"
