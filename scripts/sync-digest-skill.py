"""Rebuild the CC-Desktop scheduled-task prompt (SKILL.md) from prompts/digest.md.

The task used to say «прочитай файл digest.md», and the main model spent four
Bash turns on cat/sed before starting (04.09.2026). The prompt ends up in the
context either way, so the task prompt now embeds digest.md verbatim below a
short execution-context block. Run this after ANY edit of prompts/digest.md:

    python scripts\\sync-digest-skill.py

(python, not python3 — python3 opens the Microsoft Store on this machine.)
"""
import io
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIGEST = ROOT / "prompts" / "digest.md"
SKILL = Path(os.path.expandvars(r"%USERPROFILE%")) / ".claude" / "scheduled-tasks" / "newday-watchman" / "SKILL.md"

FRONTMATTER = """---
name: newday-watchman
description: Ежедневный новостной дайджест 09:00 (newDay) — Telegraph + пост в канал
---
"""

CONTEXT = """Ниже — полный промпт ежедневного дайджеста (копия D:\\newDay\\prompts\\digest.md,
собирается скриптом scripts\\sync-digest-skill.py — правь исходник, не эту копию).
Выполни его в точности, от начала до конца, шаг за шагом (ШАГ 1 … ШАГ 9).
Файлы читать НЕ нужно — всё, что требуется, уже здесь и в ответах тулов.

Контекст исполнения:
- Все нужные инструменты даёт MCP-сервер news-agent. Основной путь — тулы digest_context, digest_baltic_extra, publish_digest (с параметрами methodology / topics / notes / log_extra — он же закрывает прогон), плюс get_cluster, read_full, cite, verify_card; остальные (record_digest_run, send_telegram, send_telegram_comment, list_news, list_clusters, get_state, update_state, cleanup_state, publish_telegraph, fetcher_status) — только для fallback. Если инструменты не видны сразу — найди их через поиск инструментов (ToolSearch), например по слову "digest_context".
- Чтение сюжетов делегируй читателям: сабагенты через инструмент Agent с model "sonnet" и промптом «Прочитай файл D:\\newDay\\prompts\\reader.md и выполни его для события …», всех параллельно одним сообщением (ШАГ 3.5). Читатель работает через reader_packet — свою работу по чтению текстов ему не дублируй.
- Публикация, комментарий-методология, тех.лог и запись прогона — ОДИН вызов publish_digest (ШАГ 6). После него прогон закончен.
- Ответы digest_context и digest_baltic_extra читай прямо из результата тула; не перечитывай их файлами и не пересобирай скриптами. Инструменты Bash, Read, Write, Glob, Grep в этом прогоне не нужны.
- Запуск полностью автономный: не задавай вопросов, не жди подтверждений.
- Если MCP-сервер news-agent недоступен (ошибка соединения) — НИЧЕГО не публикуй и заверши работу с коротким описанием ошибки; тишина в лог-канале — сигнал владельцу.
- Если всё же понадобится запускать python-скрипты: команда называется python (НЕ python3 — это заглушка Microsoft Store).
- Сегодняшняя дата для заголовка «Дайджест DD.MM.YYYY» — реальная дата запуска; publish_digest ставит её сам.

══════════════════════════ ПРОМПТ ДАЙДЖЕСТА ══════════════════════════

"""


def main() -> int:
    digest = io.open(DIGEST, encoding="utf-8").read().lstrip("\ufeff")
    body = FRONTMATTER + "\n" + CONTEXT + digest
    if not body.endswith("\n"):
        body += "\n"
    SKILL.parent.mkdir(parents=True, exist_ok=True)
    old = io.open(SKILL, encoding="utf-8").read() if SKILL.exists() else ""
    if old == body:
        print(f"unchanged: {SKILL} ({len(body)} chars)")
        return 0
    if old:
        io.open(str(SKILL) + ".prev", "w", encoding="utf-8", newline="\n").write(old)
    io.open(SKILL, "w", encoding="utf-8", newline="\n").write(body)
    print(f"written: {SKILL} ({len(body)} chars, digest.md {len(digest)} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
