---
worth: yes
where: README.md:161
added: 2026-09-05
---
# README §7 «Регистрация в Task Scheduler» устарел

Раздел описывает три задачи с именами `NewsAgent-Fetcher/-Watchman/-Digest`.
В планировщике живут шесть с именами `Claude *`: Daily Digest, News Fetcher,
News MCP HTTP, News Tunnel, Urgent News, плюс `newsday-feedback-collector`.
HTTP-сервер MCP, cloudflared-туннель и сборщик фидбека в разделе не упомянуты,
хотя без первых двух дайджест не работает. Триггеры (watchman каждый час,
ночной guard) тоже разошлись с текстом.

Найдено при ревизии раздела «Что улучшить позже» 05.09.2026, отложено, потому
что правка чисто документационная и не связана с тем прогоном.
