# Runs the deterministic RSS fetcher.
# Schedule: every 15-30 minutes via Task Scheduler.

$ErrorActionPreference = "Continue"

# Force UTF-8 everywhere.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

# Tell Python to use UTF-8 too (in case PYTHONIOENCODING isn't set globally).
$env:PYTHONIOENCODING = "utf-8"

$root = "D:\newDay"
$logDir = Join-Path $root "logs"
$scriptsDir = Join-Path $root ".venv\scripts"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("fetcher-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

Set-Location $root
"=== $(Get-Date -Format o) === fetcher" | Out-File -FilePath $log -Append -Encoding utf8

# `2>&1 | % ToString`: PowerShell 5.1 wraps native stderr lines in
# ErrorRecords and Out-File prints them as fake "NativeCommandError"
# stack traces. ToString() unwraps them back to plain log lines.
& $scriptsDir\python.exe "$root\fetcher.py" --root $root 2>&1 |
    ForEach-Object { $_.ToString() } |
    Out-File -FilePath $log -Append -Encoding utf8
"exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8

# Event clustering (CPU-only embeddings) — reads inbox.json, writes clusters.json.
# Non-fatal: a clusterer failure must not break the fetch cycle.
"--- clusterer" | Out-File -FilePath $log -Append -Encoding utf8
& $scriptsDir\python.exe "$root\clusterer.py" --root $root 2>&1 |
    ForEach-Object { $_.ToString() } |
    Out-File -FilePath $log -Append -Encoding utf8
"exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8