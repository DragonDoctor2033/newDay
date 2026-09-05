"""search_news() over inbox + archive.jsonl, and the archive fallback in
cite() / read_full(). Throwaway NEWS_ROOT, set BEFORE import.

    D:\\newDay\\.venv\\Scripts\\python.exe scripts\\test_search_news.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="newday_search_test_"))
os.environ["NEWS_ROOT"] = str(tmp)

sys.path.insert(0, r"D:\newDay")
import mcp_server  # noqa: E402

assert mcp_server.ROOT == tmp
assert mcp_server.SOURCE_LANG, "clusterer.SOURCE_LANG did not import"


def fn(tool):
    return getattr(tool, "fn", tool)


search_news = fn(mcp_server.search_news)
cite = fn(mcp_server.cite)

now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731


def art(i, source, title, summary, hours_ago):
    return {"id": f"art_{i:08x}", "source": source, "title": title,
            "summary": summary, "url": f"https://example.org/{i}",
            "published": iso(now - timedelta(hours=hours_ago))}


inbox = [
    art(1, "TASS", "В Нарве открыли мост", "Мост через реку Нарова, ёлки", 2),
    art(2, "BBC World", "Narva bridge reopens", "Traffic across the border", 5),
    art(3, "ERR News", "Tallinn weather", "Rain all week", 1),
]
archived = [
    art(4, "BBC World", "Narva bridge closed for repairs", "Old story", 120),
    art(5, "Postimees EE", "Narva sild suletud", "Remont", 130),
]
(tmp / "inbox.json").write_text(json.dumps({"articles": inbox}), encoding="utf-8")
(tmp / "archive.jsonl").write_text(
    json.dumps(archived[0], ensure_ascii=False) + "\n"
    + "this line is not json\n"
    + json.dumps(archived[0], ensure_ascii=False) + "\n"      # duplicate → deduped
    + "\n"
    + json.dumps(archived[1], ensure_ascii=False) + "\n",
    encoding="utf-8")

# inbox + archive, newest first, where-tags, dedup of the duplicated line
r = search_news("narva")
assert [a["id"] for a in r["articles"]] == [inbox[1]["id"], archived[0]["id"], archived[1]["id"]], r
assert (r["inbox_hits"], r["archive_hits"], r["count"], r["truncated"]) == (1, 2, 3, False), r
assert [a["where"] for a in r["articles"]] == ["inbox", "archive", "archive"]
assert "summary" in r["articles"][0]

# ё/case normalisation and stem-as-prefix on Cyrillic
r = search_news("НАРВ елки")
assert [a["id"] for a in r["articles"]] == [inbox[0]["id"]], r

# all terms required
assert search_news("narva rain")["count"] == 0
assert search_news("narva traffic")["count"] == 1

# lang = source language zone
assert [a["id"] for a in search_news("нарв", lang="ru")["articles"]] == [inbox[0]["id"]]
assert search_news("narva", lang="en")["count"] == 2
assert [a["id"] for a in search_news("narva", lang="et")["articles"]] == [archived[1]["id"]]
assert search_news("narva", lang="uk")["count"] == 0

# include_archive / since_iso / source_filter / limit / compact
assert search_news("narva", include_archive=False)["count"] == 1
assert search_news("narva", since_iso=iso(now - timedelta(hours=24)))["count"] == 1
assert search_news("narva", source_filter="bbc")["count"] == 2
r = search_news("narva", limit=1, compact=True)
assert (r["count"], r["window_total"], r["truncated"]) == (1, 3, True), r
assert "summary" not in r["articles"][0]

# empty query
assert "error" in search_news("   ")

# no archive file at all → inbox only, no crash
(tmp / "archive.jsonl").unlink()
assert search_news("narva")["count"] == 1
(tmp / "archive.jsonl").write_text(json.dumps(archived[0]) + "\n", encoding="utf-8")

# cite() resolves archive ids, still reports unknown ones
r = cite([inbox[1]["id"], archived[0]["id"], "art_deadbeef"], format="markdown")
assert r["resolved"] == 2 and r["missing"] == ["art_deadbeef"], r
assert "https://example.org/4" in r["rendered"]

# read_full path: an archive id is known (not cached), an unknown id is unknown
text, cached, err = mcp_server._article_text(archived[0]["id"], allow_fetch=False)
assert (text, cached, err) == (None, False, "not cached"), (text, cached, err)
_, _, err = mcp_server._article_text("art_deadbeef", allow_fetch=False)
assert err.startswith("unknown article id"), err

print("OK test_search_news")
