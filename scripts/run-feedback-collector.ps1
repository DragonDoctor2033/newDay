# Runs the feedback collector.
# Schedule: every 15 minutes via Task Scheduler.

$ErrorActionPreference = "Continue"

# Force UTF-8 everywhere.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$env:PYTHONIOENCODING = "utf-8"

$root = "D:\newDay"
$logDir = Join-Path $root "logs"
$scriptsDir = Join-Path $root ".venv\scripts"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("feedback-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

Set-Location $root
"=== $(Get-Date -Format o) === feedback-collector" | Out-File -FilePath $log -Append -Encoding utf8

& $scriptsDir\python.exe "$root\feedback_collector.py" 2>&1 | Out-File -FilePath $log -Append -Encoding utf8
"exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8
