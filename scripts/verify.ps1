<#
.SYNOPSIS
  Read-only health and telemetry-pipeline check of the running POC.
  Exits 1 if any check fails. Changes nothing.
#>
. "$PSScriptRoot\common.ps1"
Set-Location $RepoRoot
$failed = 0

function Check($Name, [scriptblock]$Test) {
    $ok = $false
    try { $ok = [bool](& $Test) } catch { $ok = $false }
    if ($ok) { Write-Host "  PASS  $Name" -ForegroundColor Green }
    else { Write-Host "  FAIL  $Name" -ForegroundColor Red; $script:failed++ }
}
function PromValue($Query) {
    $r = Invoke-RestMethod 'http://localhost:19090/api/v1/query' -Body @{ query = $Query } -TimeoutSec 10
    if ($r.data.result.Count) { [double]$r.data.result[0].value[1] } else { $null }
}
function CanI($Verb, $Resource) {
    Invoke-Kubectl auth can-i $Verb $Resource -n aiobs-demo --as=system:serviceaccount:aiobs-demo:evidence-reader 2>$null
}

Write-Step 'Kubernetes'
Check 'node is Ready' { (Invoke-Kubectl get nodes --no-headers) -match '\sReady\s' }
foreach ($d in @('demo-api', 'aiobs-demo'), @('traffic-generator', 'aiobs-demo'), @('otel-collector', 'aiobs-observability')) {
    Check "deployment $($d[0]) is available" {
        (Invoke-Kubectl get deploy $d[0] -n $d[1] -o 'jsonpath={.status.availableReplicas}') -ge 1
    }
}
Check 'demo-api answers through the Kubernetes Service' { Test-Url 'http://localhost:18080/api/work' }

Write-Step 'Backends and UI'
Check 'Prometheus ready' { Test-Url 'http://localhost:19090/-/ready' }
Check 'Loki ready' { Test-Url 'http://localhost:13100/ready' }
Check 'Tempo ready' { Test-Url 'http://localhost:13200/ready' }
Check 'Grafana healthy' { Test-Url 'http://localhost:13000/api/health' }
Check 'Grafana dashboards provisioned' {
    (Test-Url 'http://localhost:13000/api/dashboards/uid/aiobs-health') -and (Test-Url 'http://localhost:13000/api/dashboards/uid/aiobs-incident')
}
Check 'Incident dashboard (Streamlit) healthy' { Test-Url 'http://localhost:18501/_stcore/health' }

Write-Step 'Telemetry pipelines (OpenTelemetry Collector -> backends)'
Check 'metrics: demo-api request rate > 0 in Prometheus' {
    (PromValue 'sum(rate(http_server_request_duration_seconds_count{service_name="demo-api",http_route="/api/work"}[1m]))') -gt 0
}
Check 'metrics: container metrics carry Kubernetes labels' {
    (PromValue 'count(container_memory_working_set_bytes{k8s_namespace_name="aiobs-demo",k8s_container_name="demo-api"})') -ge 1
}
Check 'logs: demo-api records in Loki within the last 2 minutes' {
    $start = [DateTimeOffset]::UtcNow.AddMinutes(-2).ToUnixTimeMilliseconds() * 1000000
    $r = Invoke-RestMethod 'http://localhost:13100/loki/api/v1/query_range' -Body @{ query = '{service_name="demo-api"}'; start = "$start"; limit = 1 } -TimeoutSec 10
    $r.data.result.Count -ge 1
}
Check 'traces: demo-api traces searchable in Tempo' {
    $r = Invoke-RestMethod 'http://localhost:13200/api/search' -Body @{ q = '{resource.service.name="demo-api"}'; limit = 1 } -TimeoutSec 10
    $r.traces.Count -ge 1
}
Check 'collector: spans, logs and metric points are being exported' {
    ((PromValue 'sum(rate(otelcol_exporter_sent_spans_total[2m]))') -gt 0) -and
    ((PromValue 'sum(rate(otelcol_exporter_sent_log_records_total[2m]))') -gt 0) -and
    ((PromValue 'sum(rate(otelcol_exporter_sent_metric_points_total[2m]))') -gt 0)
}

Write-Step 'Security boundary'
Check 'evidence-reader can read pods' { (CanI get pods) -eq 'yes' }
Check 'evidence-reader cannot delete pods' { (CanI delete pods) -eq 'no' }
Check 'evidence-reader cannot patch deployments' { (CanI patch deployments.apps) -eq 'no' }
Check 'evidence-reader cannot read secrets' { (CanI get secrets) -eq 'no' }
Check 'collector cannot use the kubelet proxy API' {
    (Invoke-Kubectl auth can-i get nodes --subresource=proxy --as=system:serviceaccount:aiobs-observability:otel-collector 2>$null) -eq 'no'
}
Check 'every published port is bound to 127.0.0.1' {
    -not (docker ps --filter 'name=aiobs' --format '{{.Ports}}' | Select-String '0\.0\.0\.0|\[::\]')
}

Write-Step 'Incident engine'
Check 'engine reads all four evidence sources' {
    $json = docker exec aiobs-dashboard python -m intelligence --minutes 2 | Out-String | ConvertFrom-Json
    -not ($json.missing_evidence | Where-Object { $null -eq $_.item })
}

if ($failed) { Write-Host "`n$failed check(s) failed." -ForegroundColor Red; exit 1 }
Write-Host "`nAll checks passed." -ForegroundColor Green
