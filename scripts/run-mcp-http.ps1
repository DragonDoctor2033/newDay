# Runs the news MCP server over streamable HTTP (127.0.0.1:8787, bearer auth)
# so cloud routines can reach it through the tunnel.
# Schedule: at system startup via Task Scheduler; loop restarts on crash.

$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$root = "D:\newDay"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "mcp-http.log"

while ($true) {
    "=== $(Get-Date -Format o) === mcp http start" | Out-File $log -Append -Encoding utf8
    & "$root\.venv\Scripts\python.exe" "$root\mcp_server.py" --http 2>&1 |
        Out-File $log -Append -Encoding utf8
    "=== $(Get-Date -Format o) === mcp http exited ($LASTEXITCODE), restart in 10s" |
        Out-File $log -Append -Encoding utf8
    Start-Sleep -Seconds 10
}
