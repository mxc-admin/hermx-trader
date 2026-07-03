$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path ".env")) {
  Write-Host "Missing .env. Copy setup/env.example to .env and fill required values first."
  exit 1
}

if (-not (Test-Path "engine-config.json")) {
  Copy-Item "config/runtime.demo.json" "engine-config.json"
  Write-Host "Created engine-config.json from config/runtime.demo.json"
}

Get-Content ".env" | ForEach-Object {
  if ($_ -match "^\s*#" -or $_ -notmatch "=") { return }
  $parts = $_ -split "=", 2
  [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
}

$env:SHADOW_ROOT = $Root
$env:HERMX_ROOT = $Root
if (-not $env:HERMX_RECEIVER_PORT) {
    if ($env:SHADOW_PORT) { $env:HERMX_RECEIVER_PORT = $env:SHADOW_PORT }
    else { $env:HERMX_RECEIVER_PORT = "8891" }
}
$env:SHADOW_PORT = $env:HERMX_RECEIVER_PORT

python "src/webhook_receiver.py"
