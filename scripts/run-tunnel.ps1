# Runs the cloudflared named tunnel (public hostname -> 127.0.0.1:8787, see cloudflared.yml)
# so cloud routines can reach the local news MCP server.
# Schedule: at logon via Task Scheduler; loop restarts on crash.

$ErrorActionPreference = "Continue"
$root = "D:\newDay"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir "tunnel.log"

while ($true) {
    "=== $(Get-Date -Format o) === tunnel start" | Out-File $log -Append -Encoding utf8
    & "$root\bin\cloudflared.exe" --config "$root\cloudflared.yml" tunnel run news-mcp 2>&1 |
        Out-File $log -Append -Encoding utf8
    "=== $(Get-Date -Format o) === tunnel exited ($LASTEXITCODE), restart in 10s" |
        Out-File $log -Append -Encoding utf8
    Start-Sleep -Seconds 10
}
