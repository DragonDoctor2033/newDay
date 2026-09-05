# News Agent — установка и запуск

Полностью автономная система: fetcher → MCP-сервер → claude -p по cron.
Никакого Cowork, никакого Claude Desktop. Биллится через Max-подписку.

## 0. Что у нас по итогу

```
D:\newDay\
├── config.json          ← секреты и настройки (в git не попадает)
├── config.example.json  ← шаблон config.json без секретов
├── sources.txt          ← RSS-источники
├── inbox.json           ← fetcher пишет, MCP читает
├── clusters.json        ← clusterer пишет, MCP читает (событийные кластеры)
├── state.json           ← MCP читает/пишет атомарно
├── archive.jsonl        ← новости старше 72 часов
├── fetch_errors.json    ← последний отчёт фетчера
├── fetcher.py           ← парсер RSS
├── doh.py               ← DNS-over-HTTPS шим для fetcher (опционально)
├── clusterer.py         ← событийная кластеризация (эмбеддинги, CPU-only)
├── mcp_server.py        ← FastMCP сервер
├── requirements.txt
├── .mcp.json            ← конфиг для claude -p
├── prompts\
│   ├── watchman.md      ← промпт для алертов (часовой)
│   ├── digest.md        ← промпт для дайджеста (раз в сутки)
│   └── reader.md        ← промпт читателя-сабагента (Sonnet), один сюжет
├── scripts\
│   ├── run-fetcher.ps1            ← каждые 15 мин (fetcher + clusterer)
│   ├── run-watchman.ps1           ← раз в час (ночью реже)
│   ├── run-mcp-http.ps1           ← MCP-сервер по HTTP, с логона, в цикле
│   ├── run-tunnel.ps1             ← cloudflared-туннель к нему, с логона, в цикле
│   ├── run-feedback-collector.ps1 ← каждые 15 мин, без LLM
│   ├── run-digest.ps1             ← заглушка: дайджест переехал в задачу CC-Desktop
│   └── sync-digest-skill.py       ← собирает SKILL.md задачи дайджеста из prompts/digest.md
├── experiments\         ← лаборатория кластеризации (cluster_lab.py)
├── cache\               ← кеш read_full + embeddings.npz
└── logs\                ← логи запусков
```

## 1. Установка зависимостей

```powershell
cd D:\newDay
python -m pip install -r requirements.txt
```

Проверь что Python в PATH (нужен 3.11+):
```powershell
python --version
```

## 2. Заполни config.json

Скопируй `config.example.json` в `config.json` и заполни:

- `tg_bot_token` — от @BotFather
- `tg_chat_id` — основной канал «News Claude»
- `tg_log_chat_id` — лог-канал «Log Claude News»
- `tg_test_chat_id` — тестовый канал (создай если ещё нет)
- `telegraph_token` — получить так:

  ```powershell
  curl "https://api.telegra.ph/createAccount?short_name=ClaudeNews&author_name=Claude%20News"
  ```
  В ответе будет `access_token` — его сюда.

- `mcp_bearer_token` — любая длинная случайная строка; её ждёт HTTP-режим
  MCP-сервера (`scripts/run-mcp-http.ps1`) от облачных рутин
- `doh_resolver_url` — (опционально) URL DoH-резолвера вида
  `https://host/dns-query`. Если задан, fetcher резолвит все хосты через него
  в обход системного DNS (помогает при DNS-блокировках провайдера). Пусто —
  DoH выключен, используется системный DNS. Можно задать и переменной
  окружения `NEWS_DOH_URL`, она имеет приоритет над config.json.
- `test_mode: true` — пока тестируем, потом переключим в false

`config.json` в `.gitignore` — токены в репозиторий не уходят.

## 3. Первый прогон fetcher вручную

```powershell
cd D:\newDay
python fetcher.py
```

Должно появиться:
- `inbox.json` с массивом статей
- `fetch_errors.json` с отчётом
- В консоли — `+N` для каждого источника
- В начале лога — `DoH active (all hosts via ...)` если резолвер задан,
  иначе `DoH unavailable — falling back to system DNS` (это нормально)

Если какие-то источники постоянно дают ошибку — закомментируй их в
`sources.txt` (поставь `#` в начале строки) или замени URL.

## 4. Проверь MCP-сервер вручную

```powershell
python mcp_server.py
```

Сервер запустится в stdio-режиме — он будет ждать команд через stdin.
Просто проверь что не упал с ошибкой импорта. Ctrl+C для выхода.

## 5. Тестовый прогон watchman

```powershell
cd D:\newDay
.\scripts\run-watchman.ps1
```

Что должно произойти (с 04.09.2026 — два вызова):
- Claude Code запустится в headless режиме (Sonnet 5, --effort high,
  --strict-mcp-config: только news-agent)
- watchman_context → окно, alerted_24h, alerts_today, очередь, кластеры
  строками (вместо get_state + cleanup_state + list_clusters)
- применит четыре фильтра; get_cluster только для o=1-кандидатов
- watchman_finish → алерты в основной канал + комментарий-методология
  под каждым + лог решений + патч state одним вызовом
- ночью (01–07) скрипт сам пропускает часы, кроме 03:00 и 06:00

Лог запуска будет в `D:\newDay\logs\watchman-YYYY-MM-DD.log`.

Если что-то пошло не так — там видно где упало.

## 6. Тестовый прогон digest

Дайджест живёт не в Task Scheduler, а в задаче Claude Desktop (см. §7);
`scripts\run-digest.ps1` — заглушка, которая только пишет строку в
`logs\digest-migrated.log`. Тестовый прогон — кнопка «Run now» у задачи
в Code-табе → Routines.

Что должно произойти (пайплайн с 02.09.2026 — механика на сервере):
- digest_context + digest_baltic_extra → карта суток по кластерам,
  балтийский добор, anti-repeat индекс с маркерами ↻/~ в строках кластеров, статистика
- ШАГ 3.5: читатели — сабагенты Agent(model=sonnet) по промпту prompts/reader.md,
  по одному на Baltic/World-сюжет, параллельно; каждый делает три вызова:
  reader_packet (кластер + близнецы + полные тексты 4 статей разных
  редакций одним ответом) → verify_card (цитаты/числа/расхождения, которых
  нет в текстах, выбрасываются) → карта
- publish_digest(структура JSON, methodology, topics, notes) → сервер
  рендерит Telegraph-HTML, публикует, шлёт пост в канал, комментарий-
  методологию под ним, тех.лог в лог-канал и записывает прогон в state —
  всё одним вызовом (с 04.09.2026; до этого 7.5/8/9 были отдельными
  вызовами, и прогон 03.09 оборвался после публикации, не записав прогон)
- record_digest_run / send_telegram_comment / send_telegram — fallback
- промпт задачи планировщика (SKILL.md) собирается из prompts/digest.md скриптом
  scripts/sync-digest-skill.py — после правки digest.md запускать его; модель
  больше не читает файл в начале прогона (04.09 это стоило 4 хода)
- prompts/digest.md переписан 05.09.2026 компактно (33k → ~29k знаков) после
  двух разборов прогонов, без потери правил: ИНСТРУМЕНТЫ → ШАГ 1–9 → блок
  «ИСТОЧНИКИ И ЦИТАТЫ» (все правила про цитаты, язык, [[art_id]], sources и
  инлайн-атрибуцию в одном месте); anti-repeat целиком в 3.0 (общие режимы
  A/B/C) и 4.0 (ужесточения World/Tech, войны); исторические пояснения
  («05.09 так случилось…») из промпта убраны — мотивация правил живёт в
  памяти проекта и в git-истории. Заголовки «ШАГ N» — стабильные
  перекрёстные ссылки, не перенумеровывать
Старые тулы (list_news, publish_telegraph, update_state, …) остались как
fallback. Серверные тесты: `.venv/Scripts/python.exe scripts/test_digest_tools.py`.

Открой Telegraph-ссылку — проверь:
- TOC сверху
- Якори #Baltic / #World / #Tech кликаются
- Все ссылки рабочие (потому что они теперь не из памяти LLM)

## 7. Регистрация задач

Актуально на 05.09.2026. Всего шесть задач в Task Scheduler и одна в Claude
Desktop; ниже — что реально зарегистрировано, а не «как задумывалось».

### Task Scheduler (пять рабочих задач + заглушка)

Общее для всех: пользователь `docto`, «Run whether user is logged on or
not» (хранится пароль), **обычные** права (не highest), «If the task is
already running → Do not start a new instance». Ни у одной нет автоперезапуска
при падении: периодические просто ждут следующего тика, серверные крутятся
в бесконечном цикле внутри своего .ps1.

| Задача | Триггер | Действие | Заметки |
|---|---|---|---|
| `Claude News Fetcher` | каждые 15 мин, с 08:07:45 | `powershell.exe -file "D:\newDay\scripts\run-fetcher.ps1"` | fetcher + clusterer; «only on AC» включён, «Run task as soon as possible after a scheduled start is missed» включён; лимит 72 ч |
| `Claude Urgent News` | каждый час, с 09:00 | `powershell.exe -file "D:\newDay\scripts\run-watchman.ps1"` | watchman; ночное прореживание (01–07 только 03:00 и 06:00) — внутри скрипта, триггер об этом не знает; лимит 72 ч |
| `Claude News MCP HTTP` | при логоне | `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\newDay\scripts\run-mcp-http.ps1` | `mcp_server.py --http` на 127.0.0.1:8787 с bearer-токеном; цикл перезапускает через 10 с после падения; лимит времени снят (PT0S) |
| `Claude News Tunnel` | при логоне | `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File D:\newDay\scripts\run-tunnel.ps1` | `bin\cloudflared.exe tunnel run news-mcp` по `cloudflared.yml`; тот же цикл; лимит снят |
| `newsday-feedback-collector` | каждые 15 мин, с 18:08 | `powershell.exe -NoProfile -ExecutionPolicy Bypass -File D:\newDay\scripts\run-feedback-collector.ps1` | `feedback_collector.py`: реплаи из группы обсуждений → `feedback_log.json`; без LLM; лимит 72 ч |
| `Claude Daily Digest` | ежедневно 09:00 | `powershell.exe -file "D:\newDay\scripts\run-digest.ps1"` | **заглушка**, скрипт сразу выходит с записью в `logs\digest-migrated.log`; удалить из консоли администратора: `schtasks /Delete /TN "Claude Daily Digest" /F` (из обычной сессии — Access is denied) |

Проверить всё разом:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -match 'Claude|newsday' } |
  ForEach-Object { $i = $_ | Get-ScheduledTaskInfo
    [pscustomobject]@{ Task=$_.TaskName; State=$_.State; LastRun=$i.LastRunTime;
                       Result=$i.LastTaskResult; Next=$i.NextRunTime } } | Format-Table -AutoSize
```

Result `0` — нормальный выход, `267009` (0x41301) — «ещё выполняется», для
двух серверных задач это штатное состояние.

Рестарт MCP-сервера после правки `mcp_server.py` (туннель трогать не нужно):

```powershell
Stop-ScheduledTask "Claude News MCP HTTP"; Start-ScheduledTask "Claude News MCP HTTP"
```

Зарегистрировать задачу заново, если планировщик чистый: Task Scheduler →
Create Task, имя и триггер из таблицы, Action → Start a program →
`powershell.exe` с аргументами из таблицы, Settings → «Allow task to be run
on demand», для серверных задач снять «Stop the task if it runs longer than».

### Claude Desktop (дайджест)

Дайджест — scheduled task самого Claude Desktop (Code-таб → Routines), а не
Task Scheduler: cron `8 9 * * *` (09:08 по местному; «09:00» в тексте —
округление), запускается как обычная сессия Claude Code с cwd `D:\newDay`,
permissionMode `auto`, и его прогоны видны в списке сессий.

- Промпт задачи — `%USERPROFILE%\.claude\scheduled-tasks\newday-watchman\SKILL.md`
  (имя каталога историческое). Он собирается из `prompts\digest.md` скриптом
  `python scripts\sync-digest-skill.py`; править исходник, потом запустить
  скрипт, руками SKILL.md не трогать.
- Реестр задач —
  `%APPDATA%\Claude\claude-code-sessions\<account>\<session>\scheduled-tasks.json`;
  там же поле `model` (сейчас `claude-opus-5`) и список одобренных тулов
  (`approvedPermissions`); effort — `effortLevel` в `D:\newDay\.claude\settings.json`.
- Реестр живёт в профиле приложения: переустановка Claude Desktop его
  стирает, SKILL.md при этом остаётся — задачу придётся создать заново.

## 8. Переключение в боевой режим

Когда тесты прошли:
1. Отредактируй `config.json` → `"test_mode": false`
2. Следующий запуск пойдёт в основной канал.

Откатиться обратно — поменять флаг и снова `true`. На лету, перезапуск
тасков не нужен.

## Архитектурные принципы (чтобы помнить как оно работает)

1. **Fetcher детерминистичен.** Только Python, без LLM. Парсит, дедуплицирует,
   присваивает стабильные id (sha1 от URL). Можно перезапускать сколько угодно
   раз — id у одной и той же статьи не поменяется.

1a. **Clusterer тоже детерминистичен и тоже без LLM.** Запускается после
   fetcher (в том же run-fetcher.ps1, падение некритично). Эмбеддит
   title+summary мультиязычной моделью multilingual-e5-base **строго на CPU**
   (машина игровая, GPU не трогаем), кластеризует 72ч-окно по событиям:
   базовый порог 0.90 + якорь-основатель против дрейфа центроида; комы
   >30 статей пересклеиваются внутри порогом 0.93 (storyline → события);
   кластеры-близнецы одного события на разных языках связываются через
   related_ids (кросс-языковая похожесть систематически ниже — у
   кросс-языковых кандидатов квота в related-списке). id кластера = id
   первой статьи (стабилен, пока она в окне). Векторы кешируются в
   cache/embeddings.npz — статья эмбеддится один раз в жизни. LLM видит
   кластеры через list_clusters / get_cluster; поле outlets считает
   независимые редакции (Pravda RU+UA = одна редакция), это правильная
   основа для фильтра «подтверждено ≥2 источниками».

2. **MCP — единственный канал для LLM работать с данными.** Промпт не делает
   web-fetch и не парсит RSS. Только `list_news`, `read_full`, `cite`,
   `publish_*`, `send_*`, `get/update_state`.

2a. **Механика дайджеста живёт на сервере, у модели — только суждение.**
   `digest_context` (компактная карта суток + балтийский добор по кластерам
   + готовый anti-repeat индекс и детерминированные маркеры повторов),
   `publish_digest` (JSON-структура → HTML-шаблон Telegraph + пост в канал),
   `record_digest_run` (запись прогона из серверного кеша digest_run_cache.json).
   До этого модель сама ужимала 67 КБ кластеров скриптами, матчила 67 КБ
   балтийских лент по заголовкам, строила индекс из 33 КБ state и писала
   13 КБ HTML руками — ~половина 10-минутного прогона.

2b. **Читатели проверяются кодом, не доверием.** Sonnet-читатели (prompts/reader.md)
   дают ×5 покрытие статей, но в эксперименте 02.09 выдумывали «дословные»
   цитаты и складывали числа сами. Поэтому `verify_card` проверяет каждую
   цитату подстрокой по тексту статьи (кеш read_full, до 60 КБ), число —
   как отдельное число в тексте, а «расхождение» требует двух проверенных
   цитат из двух статей. Что не нашлось — выбрасывается до того, как
   попадёт главной модели.

3. **URL никогда не покидают MCP-сервер в виде «голого» текста.** LLM получает
   только id. Когда нужно процитировать — ставит `[[art_xxx]]` плейсхолдер,
   сервер при отправке/публикации заменяет на реальную ссылку. Галлюцинации
   URL невозможны архитектурно.

4. **Состояние атомарно.** state.json пишется через temp + rename. Никаких
   race conditions между fetcher / watchman / digest.

5. **Fallback-логика в промпте.** Если Telegraph упал — отправь короткую
   версию в TG. Если list_news пустой — не отправляй ничего, пиши в лог.

## Если что-то сломалось

- Логи: `D:\newDay\logs\` — отдельный файл на каждый день и каждый таск
- Тех.отчёт fetcher: `D:\newDay\fetch_errors.json` — последний прогон
- Состояние: `D:\newDay\state.json` — можно глазами посмотреть что watchman помнит
- Inbox: `D:\newDay\inbox.json` — посмотреть что fetcher собрал

## Стоимость

Биллится через подписку (Max 5x). API-биллинг = 0 (`ANTHROPIC_API_KEY`
чистится в скриптах). Что реально стоит прогон — в API-эквиваленте по
usage из транскриптов сессий (~/.claude/projects/D--newDay/*.jsonl):

- до 04.09.2026: watchman (Sonnet, 12–16 ходов, 24/сутки) ~$12/сутки;
  дайджест на Fable 5.1 (43 хода, xhigh) ~$29 + 9 читателей по 28 ходов
  ~$11 — итого ~$52/сутки, три четверти всего расхода подписки.
- с 04.09.2026: дайджест на Opus 5 (модель задачи в реестре
  %APPDATA%\Claude\claude-code-sessions\…\scheduled-tasks.json), effort
  high (.claude/settings.json → effortLevel), читатели по 3 вызова,
  publish_digest закрывает прогон одним вызовом; watchman на двух тулах,
  --effort high, ночью 19 прогонов вместо 24. Ожидание ~$8–10/сутки.

Главный принцип: платят не за модель, а за ходы — на каждом ходу агент
заново отправляет весь контекст (40–60k токенов обвязки Claude Code +
всё накопленное). Меньше ходов = дешевле при той же модели.

## Что улучшить позже

- Добавить парсинг HTML-источников без RSS (через BeautifulSoup в fetcher.py)
- Добавить tool `search_news(query, lang)` в MCP — поиск по inbox+archive
