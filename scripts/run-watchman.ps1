# Runs the watchman agent via claude -p.
# Schedule: hourly via Task Scheduler.

$ErrorActionPreference = "Continue"

# Force UTF-8 everywhere so cyrillic doesn't break in logs.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$root = "D:\newDay"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("watchman-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

# IMPORTANT: clear any API key inherited from parent shell so the OAuth
# subscription auth path is used. Without this, claude -p may bill as API.
$env:ANTHROPIC_API_KEY = $null

# Night thinning (04.09.2026): between 01:00 and 07:59 local only the 03:00
# and 06:00 runs go through (00:00 and 08:00+ are hourly as before). The
# window since state.last_watchman_run covers the gap, so nothing is lost —
# only night alert latency grows to <=3h. Delete this block to go back to
# hourly (the Task Scheduler trigger itself stays hourly).
$hour = (Get-Date).Hour
if ($hour -ge 1 -and $hour -le 7 -and ($hour % 3) -ne 0) {
    "=== $(Get-Date -Format o) === skipped: night thinning (hour $hour)" |
        Out-File -FilePath $log -Append -Encoding utf8
    exit 0
}

Set-Location $root
$prompt = Get-Content "$root\prompts\watchman.md" -Raw -Encoding utf8

"=== $(Get-Date -Format o) === watchman start" | Out-File -FilePath $log -Append -Encoding utf8

# --dangerously-skip-permissions: needed for headless. The MCP server is local
#   and read-only on inbox.json, atomic on state.json — safe for our use.
# --mcp-config: explicit path so claude knows where to find the news-agent MCP.
# --strict-mcp-config: ONLY that server — the user-level MCP servers (Gmail,
#   calendar, browser, …) are useless here and their tool lists cost context.
# --effort high: Claude Code defaults to xhigh; the watchman's 4-filter check
#   does not need it, and thinking tokens are the priciest line (04.09.2026).
# Prompt goes in via stdin, not as a positional argument: the CLI
# (2.1.251+) parses tokens like "-18%" inside a positional prompt as options
# ("error: unknown option '-18%'"), which has killed every hourly run since the CLI
# auto-update of 29.08.2026. Piping avoids argument parsing entirely.
$prompt | & claude -p `
    --dangerously-skip-permissions `
    --mcp-config "$root\.mcp.json" `
    --strict-mcp-config `
    --effort high `
    --model "claude-sonnet-5" `
    2>&1 | Out-File -FilePath $log -Append -Encoding utf8

"=== $(Get-Date -Format o) === watchman exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8