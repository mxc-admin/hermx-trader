$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    if ($_ -match "^\s*#" -or $_ -notmatch "=") { return }
    $parts = $_ -split "=", 2
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
  }
}

if (-not (Test-Path "engine-config.json")) {
  Copy-Item "config/runtime.demo.json" "engine-config.json"
  Write-Host "Created engine-config.json from config/runtime.demo.json"
}

$env:HERMX_ROOT = $Root
$env:HERMX_ROOT = $Root
if (-not $env:HERMX_DASHBOARD_PORT) {
    if ($env:CLEAN_DASHBOARD_PORT) { $env:HERMX_DASHBOARD_PORT = $env:CLEAN_DASHBOARD_PORT }
    else { $env:HERMX_DASHBOARD_PORT = "8098" }
}
$env:CLEAN_DASHBOARD_PORT = $env:HERMX_DASHBOARD_PORT

python "src/dashboard.py"
