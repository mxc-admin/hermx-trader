# smoke_run.ps1 — PowerShell parity for the Windows VPS.
#
# Boots the webhook receiver + dashboard against the DEMO profile for manual
# smoke testing, with OKX order submission FORCED OFF (kill switch armed).
#
# REFACTOR_PLAN.md:181 — run receiver + dashboard against the demo profile
# locally for manual smoke testing.
#
# SAFETY: exports $env:HERMX_SUBMIT_ENABLED="false", which hard-blocks all OKX
# order submission before any subprocess is spawned (skills/emergency-stop.md,
# Level 0). This is a smoke-test harness, never a live launcher.
#
# Usage:
#   .\scripts\smoke_run.ps1            boot both services (dry-run, no submit)
#   .\scripts\smoke_run.ps1 -Check     validate prerequisites, do not launch
#   .\scripts\smoke_run.ps1 -Help      show help

param(
  [switch]$Check,
  [switch]$Help
)

$ErrorActionPreference = "Stop"

$Root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $Root

if ($Help) {
  Write-Host "smoke_run.ps1 — boot receiver + dashboard (DEMO, dry-run/no-submit)"
  Write-Host "  -Check   validate prerequisites without launching"
  Write-Host "  -Help    show this help"
  exit 0
}

# --- pick a python interpreter: prefer repo venv ----------------------------
$Python = "python"
if (Test-Path (Join-Path $Root ".venv\Scripts\python.exe")) {
  $Python = (Join-Path $Root ".venv\Scripts\python.exe")
}

# --- defaults ---------------------------------------------------------------
if (-not $env:SHADOW_PORT) { $env:SHADOW_PORT = "8891" }
if (-not $env:CLEAN_DASHBOARD_PORT) { $env:CLEAN_DASHBOARD_PORT = "8098" }

# --- load .env (ignore comments / blank lines) ------------------------------
if (Test-Path ".env") {
  Get-Content ".env" | ForEach-Object {
    if ($_ -match "^\s*#" -or $_ -notmatch "=") { return }
    $parts = $_ -split "=", 2
    [Environment]::SetEnvironmentVariable($parts[0].Trim(), $parts[1].Trim(), "Process")
  }
}

# --- --check: validate prerequisites, do not launch -------------------------
if ($Check) {
  $ok = $true
  Write-Host "smoke_run -Check (repo: $Root)"
  Write-Host "  python:         $Python"

  if (Test-Path ".env") { Write-Host "  .env:           present" }
  else { Write-Host "  .env:           MISSING (services may fail without credentials)" }

  if (Test-Path "config/runtime.demo.json") { Write-Host "  demo profile:   present" }
  else { Write-Host "  demo profile:   MISSING (config/runtime.demo.json)"; $ok = $false }

  foreach ($src in @("src/webhook_receiver.py", "src/dashboard.py")) {
    if (-not (Test-Path $src)) { Write-Host "  $src: MISSING"; $ok = $false; continue }
    & $Python -c "import ast,sys; ast.parse(open(sys.argv[1]).read())" $src 2>$null
    if ($LASTEXITCODE -eq 0) { Write-Host "  $src: parses OK" }
    else { Write-Host "  $src: SYNTAX ERROR"; $ok = $false }
  }

  Write-Host "  SHADOW_PORT:    $($env:SHADOW_PORT)"
  Write-Host "  DASHBOARD_PORT: $($env:CLEAN_DASHBOARD_PORT)"
  Write-Host "  HERMX_SUBMIT_ENABLED would be forced to: false (dry-run / no submit)"
  if ($ok) { Write-Host "CHECK: OK"; exit 0 }
  else { Write-Host "CHECK: FAILED"; exit 1 }
}

# --- ensure engine-config.json exists (copy from demo profile if missing) ----
if (-not (Test-Path "engine-config.json")) {
  Copy-Item "config/runtime.demo.json" "engine-config.json"
  Write-Host "Created engine-config.json from config/runtime.demo.json"
}

$env:SHADOW_ROOT = $Root
# SAFETY: force the global kill switch ON for smoke runs.
$env:HERMX_SUBMIT_ENABLED = "false"

Write-Host "============================================================"
Write-Host " HermX SMOKE RUN - DEMO profile"
Write-Host " DRY-RUN / NO-SUBMIT: HERMX_SUBMIT_ENABLED=false (kill switch ARMED)"
Write-Host " No OKX orders can be submitted in this mode."
Write-Host "------------------------------------------------------------"
Write-Host " python:    $Python"
Write-Host " receiver:  http://127.0.0.1:$($env:SHADOW_PORT)"
Write-Host " dashboard: http://127.0.0.1:$($env:CLEAN_DASHBOARD_PORT)"
Write-Host "============================================================"

$receiver = Start-Process -FilePath $Python -ArgumentList "src/webhook_receiver.py" -NoNewWindow -PassThru
Write-Host "receiver  PID=$($receiver.Id)  (http://127.0.0.1:$($env:SHADOW_PORT))"

$dashboard = Start-Process -FilePath $Python -ArgumentList "src/dashboard.py" -NoNewWindow -PassThru
Write-Host "dashboard PID=$($dashboard.Id)  (http://127.0.0.1:$($env:CLEAN_DASHBOARD_PORT))"

Write-Host "Both services up. Press Ctrl-C to stop."
try {
  Wait-Process -Id $receiver.Id, $dashboard.Id
}
finally {
  Write-Host ""
  Write-Host "Shutting down smoke run..."
  foreach ($p in @($dashboard, $receiver)) {
    if ($p -and -not $p.HasExited) {
      Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
    }
  }
  Write-Host "Stopped."
}
