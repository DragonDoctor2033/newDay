# Runs the daily digest via claude -p.
# Schedule: daily at 09:00 via Task Scheduler.

# ── MIGRATED 12.06.2026: дайджест теперь идёт задачей планировщика Claude Code
# Desktop (taskId "newday-watchman", описание «Ежедневный дайджест 09:00»,
# Code-таб → Routines; ежедневно в 09:08, биллинг — подписка). Изначальная
# причина (отдельный бюджет для headless claude -p с 15.06.2026) отменена,
# всё снова биллится по подписке; оставлено на CC-Desktop как есть.
# ОТКАТ: удалить этот exit-блок — и старый путь заработает как раньше
# (выключить Task Scheduler задачу "Claude Daily Digest" без админа нельзя).
"=== $(Get-Date -Format o) === skipped: migrated to CC-Desktop scheduled task" |
    Out-File -FilePath "D:\newDay\logs\digest-migrated.log" -Append -Encoding utf8
exit 0
# ──────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Continue"

# Force UTF-8 everywhere so cyrillic doesn't break in logs.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
chcp 65001 > $null

$root = "D:\newDay"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir ("digest-" + (Get-Date -Format "yyyy-MM-dd") + ".log")

$env:ANTHROPIC_API_KEY = $null

Set-Location $root
$prompt = Get-Content "$root\prompts\digest.md" -Raw -Encoding utf8

"=== $(Get-Date -Format o) === digest start" | Out-File -FilePath $log -Append -Encoding utf8

# Prompt goes in via stdin, not as a positional argument: the CLI
# (2.1.251+) parses tokens like "-18%" inside a positional prompt as options
# ("error: unknown option '-18%'"), which has killed every hourly run since the CLI
# auto-update of 29.08.2026. Piping avoids argument parsing entirely.
$prompt | & claude -p `
    --dangerously-skip-permissions `
    --mcp-config "$root\.mcp.json" `
    --model "claude-fable-5[1m]" `
    2>&1 | Out-File -FilePath $log -Append -Encoding utf8

"=== $(Get-Date -Format o) === digest exit=$LASTEXITCODE" | Out-File -FilePath $log -Append -Encoding utf8