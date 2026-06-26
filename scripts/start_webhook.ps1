$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if (-not (Test-Path ".env")) {
  Write-Host "Missing .env. Copy setup/env.example to .env and fill required values first."
  exit 1
}

if (-not (Test-Path "shadow-config.json")) {
  Copy-Item "config/runtime.demo.json" "shadow-config.json"
  Write-Host "Created shadow-config.json from config/runtime.demo.json"
}

Get-Content ".env" | ForEach-Object {
  if ($_ -match "^\s*#" -or $_ -notmatch "=") { return }
  $parts = $_ -split "=", 2
  [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
}

$env:SHADOW_ROOT = $Root
if (-not $env:SHADOW_PORT) { $env:SHADOW_PORT = "8888" }

python "src/webhook_receiver.py"
