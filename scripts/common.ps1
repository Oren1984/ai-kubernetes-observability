# Shared settings for the local operations scripts. Dot-source, do not run.
$ErrorActionPreference = 'Stop'

$RepoRoot    = Split-Path -Parent $PSScriptRoot
$ClusterName = 'aiobs'
$NodeName    = "$ClusterName-control-plane"
$RuntimeDir  = Join-Path $RepoRoot '.runtime'
$Kubeconfig  = Join-Path $RuntimeDir 'kubeconfig'
$KindVersion = 'v0.33.0'
$AppImage    = 'aiobs/demo-api:dev'

# A project-local kubeconfig: the user's ~/.kube/config is never touched.
$env:KUBECONFIG = $Kubeconfig

$Urls = [ordered]@{
    'Incident dashboard (Streamlit)' = 'http://localhost:18501'
    'Grafana'                        = 'http://localhost:13000'
    'Prometheus'                     = 'http://localhost:19090'
    'Loki'                           = 'http://localhost:13100'
    'Tempo'                          = 'http://localhost:13200'
    'demo-api (Kubernetes NodePort)' = 'http://localhost:18080'
}

function Write-Step($Message) { Write-Host "`n==> $Message" -ForegroundColor Cyan }

# kubectl runs inside the kind node, not on the host. Host antivirus with HTTPS
# inspection (seen with Norton) intermittently re-signs the TLS session to the
# published API port, which makes host-side kubectl fail certificate checks.
function Invoke-Kubectl {
    docker exec $NodeName kubectl --kubeconfig /etc/kubernetes/admin.conf @args
}
function Invoke-NodeShell($Command) {
    docker exec -e KUBECONFIG=/etc/kubernetes/admin.conf $NodeName sh -c $Command
}

function Assert-Command($Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required tool '$Name' was not found on PATH."
    }
}

# kind from PATH if present, otherwise a checksum-verified copy in .tools/.
# Only cluster creation and deletion need it.
function Get-Kind {
    $onPath = Get-Command kind -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    $local = Join-Path $RepoRoot '.tools\kind.exe'
    if (-not (Test-Path $local)) {
        Write-Step "Downloading kind $KindVersion into .tools\"
        New-Item -ItemType Directory -Force (Split-Path $local) | Out-Null
        $base = "https://github.com/kubernetes-sigs/kind/releases/download/$KindVersion"
        Invoke-WebRequest "$base/kind-windows-amd64" -OutFile $local
        $sum = [Text.Encoding]::ASCII.GetString((Invoke-WebRequest "$base/kind-windows-amd64.sha256sum").Content)
        if ($sum.Trim().Split(' ')[0] -ne (Get-FileHash $local -Algorithm SHA256).Hash.ToLower()) {
            Remove-Item $local
            throw 'kind download failed checksum verification.'
        }
    }
    if (-not (Test-Path $local)) {
        throw "kind.exe disappeared from .tools\ after download (antivirus quarantine?). Install it yourself, e.g. 'winget install Kubernetes.kind', and re-run."
    }
    return $local
}

function Test-Url($Url, $TimeoutSec = 5) {
    try {
        return (Invoke-WebRequest $Url -TimeoutSec $TimeoutSec -SkipHttpErrorCheck).StatusCode -eq 200
    } catch { return $false }
}

function Wait-Until([scriptblock]$Condition, $What, $TimeoutSec = 180) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if (& $Condition) { Write-Host "  ok  $What"; return }
        Start-Sleep -Seconds 3
    }
    throw "Timed out after ${TimeoutSec}s waiting for: $What"
}
