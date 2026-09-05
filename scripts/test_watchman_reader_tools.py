"""Tests for the server-side reader/watchman pipeline tools:
reader_packet / watchman_context / watchman_finish / _md_to_html_basic /
publish_digest (one-step finish path).

Runs against a throwaway NEWS_ROOT (set BEFORE import) so the real
state.json / inbox / clusters / cache are never touched. Exercises the raw
functions behind the FastMCP tool objects. Network is never touched:
mcp_server._fetch_article_text, mcp_server._publish_telegraph_html,
mcp_server._send_telegram_text and mcp_server._send_telegram_comment are
monkeypatched.

    D:\\newDay\\.venv\\Scripts\\python.exe scripts\\test_watchman_reader_tools.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="newday_watchman_reader_test_"))
os.environ["NEWS_ROOT"] = str(tmp)

sys.path.insert(0, r"D:\newDay")
import mcp_server  # noqa: E402  (must come after NEWS_ROOT is set)

assert mcp_server.ROOT == tmp, f"NEWS_ROOT not honored: {mcp_server.ROOT}"


def fn(tool):
    return getattr(tool, "fn", tool)


reader_packet = fn(mcp_server.reader_packet)
watchman_context = fn(mcp_server.watchman_context)
watchman_finish = fn(mcp_server.watchman_finish)
publish_digest = fn(mcp_server.publish_digest)

now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
iso_off = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731

failures = []


def check(cond, msg):
    if not cond:
        failures.append(msg)
        print("FAIL:", msg)
    else:
        print("ok:  ", msg)


# Never let a test hit the network: any article without a cache/<id>.txt
# file "fails to fetch" the same way a paywalled/blocked source would.
def _no_network_fetch(art):
    return {"error": "HTTP 403"}


mcp_server._fetch_article_text = _no_network_fetch


def art(id_, source, title, hours_ago, summary="s"):
    return {"id": id_, "source": source, "title": title, "summary": summary,
            "published": iso_off(now - timedelta(hours=hours_ago)),
            "url": f"https://example.org/{id_}"}


def cache_text(art_id, text):
    (tmp / "cache" / f"{art_id}.txt").write_text(text, encoding="utf-8")


def A(i):
    return f"art_{i:08x}"


# ════════════════════════ seed: inbox.json ════════════════════════
# id ranges: 0x01-0x0f digest content, 0x10-0x1f fetch-fallback cluster,
# 0x20-0x2f editorial-camp cluster, 0x30-0x3f twins, 0x40-0x4f max_chars,
# 0x50-0x5f budget, 0x60-0x6f expired.

digest_articles = [
    art(A(1), "TASS", "Big world event", 3),
    art(A(2), "BBC World", "Big world event (BBC)", 2),
    art(A(3), "ERR est", "Kohalik uudis", 5),
    art(A(4), "Postimees RU", "Локальная новость", 4),
    art(A(5), "ERR est", "Singleton local", 1),
]

fail_articles = [
    art(A(0x11), "Guardian World", "Fail candidate (western)", 1),   # no cache -> 403
    art(A(0x12), "ERR est", "Baltic candidate", 2),                  # cached
    art(A(0x13), "TASS", "Russian candidate", 3),                    # cached
    art(A(0x14), "Unian RU", "Ukrainian candidate (reserve)", 4),    # cached
]

camp_articles = [
    art(A(0x21), "ERR est", "ERR est story", 1),
    art(A(0x22), "ERR rus", "ERR rus story", 2),
    art(A(0x23), "Postimees RU", "Postimees story", 3),
    art(A(0x24), "TASS", "TASS story", 1),
    art(A(0x25), "RIA", "RIA story", 2),
    art(A(0x26), "Guardian World", "Guardian story", 1),
    art(A(0x27), "Unian RU", "Unian story", 1),
]

twin_articles = [
    art(A(0x31), "ERR est", "Twin main article", 1),
    art(A(0x32), "TASS", "Twin twin article", 1),
    art(A(0x33), "Guardian World", "Related (not twin) article", 1),
]

maxchars_articles = [
    art(A(0x41), "ERR est", "Long single article", 1),
]

budget_articles = [
    art(A(0x51), "ERR est", "Budget baltic", 1),
    art(A(0x52), "Guardian World", "Budget western", 1),
    art(A(0x53), "TASS", "Budget russian", 1),
    art(A(0x54), "Unian RU", "Budget ukrainian", 1),
]

expired_articles = [
    art(A(0x61), "ERR est", "Still-present article", 1),
    # A(0x62) is deliberately NOT put in the inbox -> "expired" article id.
]

all_articles = (digest_articles + fail_articles + camp_articles + twin_articles +
                maxchars_articles + budget_articles + expired_articles)
(tmp / "inbox.json").write_text(json.dumps({
    "generated_at": iso(now), "articles": all_articles, "counts_by_source": {}
}), encoding="utf-8")

(tmp / "config.json").write_text(json.dumps({"test_mode": True}), encoding="utf-8")
(tmp / "fetch_errors.json").write_text(json.dumps(
    {"dead_sources": ["The Baltic Times"], "errors": ["x"]}), encoding="utf-8")

# cache/<id>.txt for everything that must be fetchable without network.
cache_text(A(0x12), "ERR est baltic candidate cached text, fetched successfully.")
cache_text(A(0x13), "TASS russian candidate cached text, fetched successfully too.")
cache_text(A(0x14), "Unian RU ukrainian reserve candidate cached text.")
for aid, label in ((A(0x21), "ERR est"), (A(0x22), "ERR rus"), (A(0x23), "Postimees RU"),
                   (A(0x24), "TASS"), (A(0x25), "RIA"), (A(0x26), "Guardian World"),
                   (A(0x27), "Unian RU")):
    cache_text(aid, f"Cached body for {label}. " * 5)
cache_text(A(0x31), "Twin main cached body about the same event.")
cache_text(A(0x32), "Twin twin cached body, same event, different language camp.")
cache_text(A(0x41), "Minister said something long. " * 50)   # ~1550 chars
_long = "Lorem ipsum dolor sit amet consectetur adipiscing elit. " * 250  # ~14500 chars
for aid in (A(0x51), A(0x52), A(0x53), A(0x54)):
    cache_text(aid, _long)
cache_text(A(0x61), "Still-present article cached body.")


# ════════════════════════ Part 1a: watchman_context — no clusters.json ═══
T0 = now - timedelta(hours=1)
T0_iso = iso(T0)
yesterday = (now - timedelta(days=1)).date().isoformat()

wctx_state = {
    "alerted": [
        {"ts": iso(now - timedelta(hours=1)), "topic_key": "fresh1", "headline": "Fresh One"},
        {"ts": iso(now - timedelta(hours=30)), "topic_key": "stale1", "headline": "Stale One"},
    ],
    "queued_for_digest": [{"id": f"q{i}", "topic": f"topic{i}"} for i in range(1, 75)],
    "alerts_today": {"date": yesterday, "count": 3},
    "last_watchman_run": T0_iso,
    "last_digest_run": None,
}
(tmp / "state.json").write_text(json.dumps(wctx_state), encoding="utf-8")

check(not (tmp / "clusters.json").exists(), "precondition: clusters.json absent")
r0 = watchman_context()
check("error" in r0, "watchman_context: error when clusters.json missing")
check(r0.get("since_iso") == T0_iso, "watchman_context: since_iso present without clusters.json")
check(len(r0.get("alerted_24h") or []) == 1 and r0["alerted_24h"][0]["topic_key"] == "fresh1",
      "watchman_context: alerted_24h present/pruned without clusters.json")
on_disk = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(len(on_disk["alerted"]) == 1 and on_disk["alerted"][0]["topic_key"] == "fresh1",
      "watchman_context: stale alerted pruned on disk (side effect)")
check(on_disk.get("last_watchman_run") == T0_iso,
      "watchman_context: last_watchman_run untouched")


# ════════════════════════ seed: clusters.json ═════════════════════
def cl(cid, article_ids, *, related_ids=None, storyline_id=None, size=None,
       langs=("en",), outlets=None, first_ts=None, last_ts=None,
       title="cluster", sample_ids=None):
    return {
        "id": cid, "title": title, "size": size if size is not None else len(article_ids),
        "outlets": list(outlets) if outlets is not None else [],
        "langs": list(langs), "first_ts": first_ts or iso(now - timedelta(hours=6)),
        "last_ts": last_ts or iso(now - timedelta(hours=5)), "storyline_id": storyline_id,
        "article_ids": list(article_ids), "related_ids": list(related_ids or []),
        "sample_ids": list(sample_ids) if sample_ids is not None else list(article_ids)[:3],
    }


clusters = [
    # -- reader_packet clusters --
    cl("cl_fail", [A(0x11), A(0x12), A(0x13), A(0x14)], title="Fail-fallback cluster"),
    cl("cl_camps", [A(0x21), A(0x22), A(0x23), A(0x24), A(0x25), A(0x26), A(0x27)],
       title="Editorial camps cluster"),
    cl("cl_twin_main", [A(0x31)], related_ids=["cl_twin_twin", "cl_twin_related"],
       title="Twin main cluster"),
    cl("cl_twin_twin", [A(0x32)], title="Twin twin cluster"),
    cl("cl_twin_related", [A(0x33)], title="Related-not-twin cluster"),
    cl("cl_maxchars", [A(0x41)], title="Max-chars cluster"),
    cl("cl_budget", [A(0x51), A(0x52), A(0x53), A(0x54)], title="Budget cluster"),
    cl("cl_expired", [A(0x61), A(0x62)], title="Expired-id cluster"),
    # -- watchman_context clusters --
    cl("cl_w1", ["w1a"], outlets=["ERR"], last_ts=iso(T0 - timedelta(hours=2)),
       title="Before window", sample_ids=["w1a"]),
    cl("cl_w2", ["w2a", "w2b"], outlets=["ERR"], last_ts=iso(T0 + timedelta(minutes=10)),
       title="Baltic in window", sample_ids=["w2a", "w2b"], related_ids=["cl_w3"]),
    cl("cl_w3", ["w3a"], outlets=["BBC World"], last_ts=iso(T0 + timedelta(minutes=20)),
       title="World in window", sample_ids=["w3a"]),
    cl("cl_w4", ["w4a"], outlets=["TASS"], last_ts=iso(T0 + timedelta(minutes=30)),
       title="Russian in window", sample_ids=["w4a"]),
]
(tmp / "clusters.json").write_text(json.dumps({
    "generated_at": iso(now), "article_count": len(all_articles),
    "cluster_count": len(clusters), "multi_outlet_count": 0, "clusters": clusters,
}), encoding="utf-8")


# ════════════════════════ Part 2: reader_packet ═══════════════════

# -- unknown cluster --
r = reader_packet("cl_does_not_exist")
check(r.get("error", "").startswith("unknown cluster id"), "reader_packet: unknown cluster -> error")

# -- fetch failure -> articles_failed, next candidate takes its place --
r = reader_packet("cl_fail", max_articles=3)
failed_ids = {f["id"] for f in r["articles_failed"]}
text_ids = {t["id"] for t in r["texts"]}
check(A(0x11) in failed_ids, "reader_packet: uncached article ends up in articles_failed")
check(r["articles_failed"][0].get("error") == "HTTP 403", "reader_packet: articles_failed carries fetch error")
check(A(0x11) not in text_ids, "reader_packet: failed article not in texts")
check(len(r["texts"]) == 3, "reader_packet: fallback backfilled texts up to max_articles despite one failure")
check(text_ids == {A(0x12), A(0x13), A(0x14)},
      f"reader_packet: the 3 cached articles (incl. reserve) fill texts (got {text_ids})")

# -- editorial camps: one per camp, ERR est + ERR rus not both taken --
r = reader_packet("cl_camps", max_articles=4)
check(len(r["texts"]) == 4, "reader_packet camps: 4 texts requested and produced")
camps = {mcp_server._source_group(t["source"]) for t in r["texts"]}
check(camps == {"baltic", "western", "russian", "ukrainian"},
      f"reader_packet camps: one article per camp (got {camps})")
camp_sources = [t["source"] for t in r["texts"]]
check(not ("ERR est" in camp_sources and "ERR rus" in camp_sources),
      f"reader_packet camps: same-newsroom outlets not both taken while other camps exist (got {camp_sources})")
check(sum(1 for s in camp_sources if mcp_server._source_group(s) == "baltic") == 1,
      "reader_packet camps: exactly one baltic-camp article taken")

# -- twin_ids: merge articles, twin excluded from related_candidates --
r = reader_packet("cl_twin_main", twin_ids=["cl_twin_twin"], max_articles=2)
check(sorted(r["twins_included"]) == ["cl_twin_twin"], "reader_packet twins: twins_included")
article_ids_out = {a["id"] for a in r["articles"]}
check(article_ids_out == {A(0x31), A(0x32)}, "reader_packet twins: articles merged from cluster + twin")
related_ids_out = {c["id"] for c in r["related_candidates"]}
check("cl_twin_twin" not in related_ids_out, "reader_packet twins: the merged twin is NOT a related_candidate")
check("cl_twin_related" in related_ids_out, "reader_packet twins: the other related cluster IS a candidate")
text_ids2 = {t["id"] for t in r["texts"]}
check(text_ids2 == {A(0x31), A(0x32)},
      f"reader_packet twins: both main + twin article texts picked (different camps) (got {text_ids2})")

# -- max_chars truncation (max_chars is clamped to a floor of 1000) --
r = reader_packet("cl_maxchars", max_articles=1, max_chars=1000)
full_len = len(mcp_server.CACHE_DIR.joinpath(f"{A(0x41)}.txt").read_text(encoding="utf-8"))
t = r["texts"][0]
check(t["truncated"] is True and len(t["text"]) == 1000,
      f"reader_packet max_chars: cut to 1000 chars, truncated=True (got len={len(t['text'])})")
check(t["total_chars"] == full_len, f"reader_packet max_chars: total_chars is the full length ({full_len})")

# -- total budget across 4 long texts --
r = reader_packet("cl_budget", max_articles=4, max_chars=12000)
total = sum(len(t["text"]) for t in r["texts"])
check(total <= mcp_server.READER_TOTAL_CHARS,
      f"reader_packet budget: sum(texts[].text) <= READER_TOTAL_CHARS (got {total})")
check(len(r["texts"]) < 4,
      f"reader_packet budget: the shared budget stops before all 4 candidates are fetched (got {len(r['texts'])})")

# -- expired_ids --
r = reader_packet("cl_expired")
check(r["expired_ids"] == [A(0x62)], f"reader_packet: article id absent from inbox reported as expired (got {r['expired_ids']})")
check({a["id"] for a in r["articles"]} == {A(0x61)}, "reader_packet: only the present article is in articles")


# ════════════════════════ Part 3: watchman_context (with clusters) ════
r1 = watchman_context(limit=2)
check("error" not in r1, "watchman_context: no error once clusters.json exists")
check(r1["since_iso"] == T0_iso, "watchman_context: since_iso == state.last_watchman_run")
check(r1["clusters_count"] == 2 and r1["window_total"] == 3 and r1["truncated"] is True,
      f"watchman_context: limit respected, window_total/truncated correct (got {r1['clusters_count']}/{r1['window_total']}/{r1['truncated']})")
lines = r1["clusters"].splitlines()
check(all(cid not in r1["clusters"] for cid in ("cl_w1",)), "watchman_context: cluster before since_iso excluded")
check(lines[0].startswith("cl_w2") and lines[1].startswith("cl_w3"),
      f"watchman_context: window clusters kept in clusters.json order (got {[l.split()[0] for l in lines]})")

r2 = watchman_context(limit=10)
check(r2["clusters_count"] == 3 and r2["window_total"] == 3 and r2["truncated"] is False,
      "watchman_context: limit>=window -> not truncated")
by_id = {l.split()[0]: l for l in r2["clusters"].splitlines()}
check(" o=" in by_id["cl_w2"], "watchman_context: line carries o=")
check("s:w2a,w2b" in by_id["cl_w2"], "watchman_context: line carries s:<sample_ids>")
check(" B" in by_id["cl_w2"].split("|")[0], "watchman_context: Baltic-outlet cluster flagged B")
check(" B" not in by_id["cl_w3"].split("|")[0] and " B" not in by_id["cl_w4"].split("|")[0],
      "watchman_context: non-Baltic clusters not flagged B")

check(r2["alerts_today"] == {"date": now.date().isoformat(), "count": 0},
      f"watchman_context: alerts_today reset for a new day (got {r2['alerts_today']})")
after_reset = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(after_reset["alerts_today"] == {"date": yesterday, "count": 3},
      "watchman_context: alerts_today reset is response-only, not written to disk")

check(r2["queued_total"] == 74, f"watchman_context: queued_total counts everything (got {r2['queued_total']})")
qlines = r2["queued_recent"].splitlines()
check(len(qlines) == mcp_server.WATCHMAN_QUEUE_LINES,
      f"watchman_context: queued_recent capped at WATCHMAN_QUEUE_LINES (got {len(qlines)})")
check(qlines[0].startswith("q15 ") and qlines[-1].startswith("q74 "),
      f"watchman_context: queued_recent keeps the newest items, newest last (got first={qlines[0]!r} last={qlines[-1]!r})")

still_t0 = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(still_t0.get("last_watchman_run") == T0_iso,
      "watchman_context: last_watchman_run still untouched after multiple calls")


# ════════════════════════ Part 4: _md_to_html_basic ════════════════
h = mcp_server._md_to_html_basic
check(h("A & B < C > D") == "A &amp; B &lt; C &gt; D", "_md_to_html_basic: escapes & < >")
check(h("*bold*") == "<b>bold</b>", "_md_to_html_basic: *x* -> <b>x</b>")
check(h("`code`") == "<code>code</code>", "_md_to_html_basic: `x` -> <code>x</code>")
check(h("see [[art_deadbeef]] now") == "see [[art_deadbeef]] now",
      "_md_to_html_basic: [[art_xxxxxxxx]] placeholders untouched")
check(h("*Важно* & <тест>") == "<b>Важно</b> &amp; &lt;тест&gt;",
      "_md_to_html_basic: combined bold + escaping")


# ════════════════════════ Part 5: watchman_finish ══════════════════

def make_text_mock(calls, *, fail_when=None, start_id=1000):
    counter = {"n": start_id}

    def _mock(text, target="main", parse_mode="Markdown", disable_preview=False):
        calls.append({"kind": "text", "target": target, "parse_mode": parse_mode, "text": text})
        if fail_when and fail_when(text, target, parse_mode):
            return {"sent_chunks": 1, "missing_placeholders": [],
                    "results": [{"ok": False, "description": "Bad Request: chat not found"}]}
        counter["n"] += 1
        return {"sent_chunks": 1, "missing_placeholders": [],
                "results": [{"ok": True, "result": {"message_id": counter["n"]}}]}
    return _mock


def make_parse_fallback_text_mock(calls, *, fail_target="main", start_id=2000):
    counter = {"n": start_id}

    def _mock(text, target="main", parse_mode="Markdown", disable_preview=False):
        calls.append({"kind": "text", "target": target, "parse_mode": parse_mode, "text": text})
        if target == fail_target and parse_mode == "Markdown":
            return {"sent_chunks": 1, "missing_placeholders": [],
                    "results": [{"ok": False, "description": "Bad Request: can't parse entities"}]}
        counter["n"] += 1
        return {"sent_chunks": 1, "missing_placeholders": [],
                "results": [{"ok": True, "result": {"message_id": counter["n"]}}]}
    return _mock


def make_comment_mock(calls, *, error=None, start_id=5000):
    counter = {"n": start_id}

    def _mock(text, channel_msg_id, parse_mode="Markdown"):
        calls.append({"kind": "comment", "channel_msg_id": channel_msg_id,
                      "parse_mode": parse_mode, "text": text})
        if error is not None:
            return {"error": error}
        counter["n"] += 1
        return {"discussion_msg_id": counter["n"],
                "results": [{"ok": True, "result": {"message_id": counter["n"]}}]}
    return _mock


wf_state = {
    "alerted": [], "queued_for_digest": [], "last_watchman_run": None,
    "last_digest_run": None, "alerts_today": {"date": now.date().isoformat(), "count": 1},
}
(tmp / "state.json").write_text(json.dumps(wf_state), encoding="utf-8")

# -- (a) no alerts: log only, queued normalized, alerts_today untouched --
calls = []
mcp_server._send_telegram_text = make_text_mock(calls)
mcp_server._send_telegram_comment = make_comment_mock(calls)
res = watchman_finish(
    log_text="📋 Watchman: nothing crossed the bar this hour",
    queued=[{"id": "art_qq000001", "topic": "Explicit topic"},
           {"id": "art_qq000002", "headline": "Falls back to headline"}],
)
check(res["alerts_sent"] == 0 and res["queued_added"] == 2, "watchman_finish (no alerts): counts")
check(res["log_sent"] is True, "watchman_finish (no alerts): log sent ok")
check([c for c in calls if c["kind"] == "comment"] == [], "watchman_finish (no alerts): no comment call")
check(len([c for c in calls if c["kind"] == "text" and c["target"] == "log"]) == 1,
      "watchman_finish (no alerts): exactly one log send")
check("queued_for_digest" not in res and "digest_runs" not in res,
      "watchman_finish (no alerts): compact response, no full-state keys")
st = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(st["last_watchman_run"] == res["ts"], "watchman_finish (no alerts): last_watchman_run set")
check(st["queued_for_digest"] == [{"id": "art_qq000001", "topic": "Explicit topic"},
                                  {"id": "art_qq000002", "topic": "Falls back to headline"}],
      f"watchman_finish (no alerts): queued normalized to {{id, topic}} (got {st['queued_for_digest']})")
check(st["alerts_today"]["count"] == 1, "watchman_finish (no alerts): alerts_today not incremented")

# -- (b) one good alert: order main -> comment -> log; state.alerted record --
calls = []
mcp_server._send_telegram_text = make_text_mock(calls, start_id=3000)
mcp_server._send_telegram_comment = make_comment_mock(calls, start_id=6000)
res = watchman_finish(
    log_text="📋 Watchman: one alert",
    alerts=[{"topic_key": "tk1", "headline": "Headline 1", "text": "🚨 Alert text",
            "methodology": "Because it matters", "article_ids": [A(1), A(2)]}],
)
kinds = [(c["kind"], c.get("target")) for c in calls]
check(kinds == [("text", "main"), ("comment", None), ("text", "log")],
      f"watchman_finish (1 alert): call order main -> comment -> log (got {kinds})")
check(res["alerts_sent"] == 1 and len(res["alert_msg_ids"]) == 1, "watchman_finish (1 alert): counts")
st = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(len(st["alerted"]) == 1, "watchman_finish (1 alert): one alerted record")
rec = st["alerted"][0]
check(rec["topic_key"] == "tk1" and rec["headline"] == "Headline 1" and rec["article_ids"] == [A(1), A(2)]
     and rec["channel_msg_id"] == res["alert_msg_ids"][0] and rec["discussion_msg_id"] == 6001 and "ts" in rec,
      f"watchman_finish (1 alert): alerted record fields (got {rec})")
check(st["alerts_today"]["count"] == 2, "watchman_finish (1 alert): alerts_today incremented by 1")

# -- (c) main message fails: not recorded, in errors, other alerts still processed --
calls = []
mcp_server._send_telegram_text = make_text_mock(
    calls, fail_when=lambda text, target, pm: target == "main" and "fail" in text, start_id=4000)
mcp_server._send_telegram_comment = make_comment_mock(calls, start_id=7000)
res = watchman_finish(
    log_text="📋 Watchman: mixed",
    alerts=[
        {"topic_key": "tk_fail", "headline": "H2", "text": "🚨 fail this one",
         "methodology": "why", "article_ids": [A(1)]},
        {"topic_key": "tk_ok", "headline": "H3", "text": "🚨 ok this one",
         "methodology": "why2", "article_ids": [A(2)]},
    ],
)
check(res["alerts_sent"] == 1, f"watchman_finish (mixed): only the successful alert counted (got {res['alerts_sent']})")
check(any(e.get("alert") == "tk_fail" for e in res["errors"]),
      f"watchman_finish (mixed): failed alert reported in errors (got {res['errors']})")
st = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
new_topics = [a["topic_key"] for a in st["alerted"] if a["topic_key"] in ("tk_fail", "tk_ok")]
check(new_topics == ["tk_ok"], f"watchman_finish (mixed): only the successful alert recorded (got {new_topics})")
comment_calls = [c for c in calls if c["kind"] == "comment"]
check(len(comment_calls) == 1, f"watchman_finish (mixed): comment attempted only for the successful alert (got {len(comment_calls)})")

# -- (d) Markdown parse failure on the alert -> HTML fallback --
calls = []
mcp_server._send_telegram_text = make_parse_fallback_text_mock(calls, fail_target="main", start_id=8000)
mcp_server._send_telegram_comment = make_comment_mock(calls, start_id=9000)
res = watchman_finish(
    log_text="📋 Watchman: fallback case",
    alerts=[{"topic_key": "tk_md", "headline": "H4", "text": "🚨 *Important* update A & B",
            "methodology": "why3", "article_ids": [A(1)]}],
)
check(res["errors"] == [], f"watchman_finish (fallback): no errors after HTML retry (got {res['errors']})")
main_calls = [c for c in calls if c["kind"] == "text" and c["target"] == "main"]
check(len(main_calls) == 2, f"watchman_finish (fallback): retried once as HTML (got {len(main_calls)} main calls)")
check(main_calls[0]["parse_mode"] == "Markdown" and "*Important*" in main_calls[0]["text"],
      "watchman_finish (fallback): first attempt is the original Markdown text")
check(main_calls[1]["parse_mode"] == "HTML" and "<b>Important</b>" in main_calls[1]["text"]
     and "A &amp; B" in main_calls[1]["text"],
      f"watchman_finish (fallback): second attempt is HTML via _md_to_html_basic (got {main_calls[1]['text']!r})")
st = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(any(a["topic_key"] == "tk_md" for a in st["alerted"]),
      "watchman_finish (fallback): alert recorded once the HTML retry succeeded")


# ════════════════════════ Part 6: publish_digest one-step finish ══
baltic = [{"headline": "Local story", "toc": "Local story", "essence": "Essence [[%s]]" % A(3),
          "sides": [{"label": "ERR", "text": "a"}, {"label": "Postimees", "text": "b"}],
          "averaged": "avg", "sources": [A(3), A(4)]}]
world = [{"headline": "World", "toc": "World short", "text": "txt", "sources": [A(1), A(2)]}]
tech = [{"headline": "Tech", "text": "t", "sources": [A(2)]}]
topics = [
    {"slug": "local_story", "section": "baltic", "headline": "Local", "entities": ["E"],
     "article_ids": [A(3), A(4)], "cluster_ids": [], "depth": "deep_dive"},
    {"slug": "world_x", "section": "world", "headline": "World", "article_ids": [A(1), A(2)],
     "depth": "deep_dive"},
    {"slug": "tech_x", "section": "tech", "headline": "Tech", "article_ids": [A(2)], "depth": "deep_dive"},
]

# -- scenario 1: success end to end --
(tmp / "state.json").write_text(json.dumps({
    "alerted": [], "queued_for_digest": [{"id": "art_zzzzzzzz"}],
    "last_watchman_run": None, "last_digest_run": None,
    "alerts_today": {"date": now.date().isoformat(), "count": 0},
}), encoding="utf-8")
mcp_server._save_run_cache({
    "stats": {"articles_in_window": 9, "clusters_in_window": 5, "sources_active": 8,
              "fetch_errors": [], "dead_sources": ["The Baltic Times"]},
    "alerted_count": 1, "queued_count": 1, "anti_repeat_matched_clusters": [],
}, reset=True)

calls = []
mcp_server._publish_telegraph_html = lambda title, html: (
    calls.append({"kind": "telegraph", "title": title}) or
    {"url": "https://telegra.ph/x", "missing_placeholders": []})
mcp_server._send_telegram_text = make_text_mock(calls, start_id=10000)
mcp_server._send_telegram_comment = make_comment_mock(calls, start_id=11000)

res = publish_digest(baltic=baltic, world=world, tech=tech,
                     methodology="🤖 *Как собирался*", topics=topics,
                     notes="n", log_extra="A=1 B=2")

kinds = [(c["kind"], c.get("target")) for c in calls]
check(kinds == [("telegraph", None), ("text", "main"), ("comment", None), ("text", "log")],
      f"publish_digest: telegraph -> main post -> comment -> tech log, in order (got {kinds})")
check(res.get("url") == "https://telegra.ph/x", "publish_digest: url in response")
check(res.get("channel_msg_id") == 10001, f"publish_digest: channel_msg_id from main post mock (got {res.get('channel_msg_id')})")
check(res.get("comment", {}).get("discussion_msg_id") == 11001,
      f"publish_digest: comment.discussion_msg_id from mock (got {res.get('comment')})")
check(res.get("tech_log_ok") is True, "publish_digest: tech_log_ok True")
tech_log_text = [c["text"] for c in calls if c["kind"] == "text" and c["target"] == "log"][0]
check("https://telegra.ph/x" in tech_log_text, "publish_digest: tech log mentions the published url")
check("Digest run" in tech_log_text, "publish_digest: tech log has the 'Digest run' line")
check("A=1 B=2" in tech_log_text, "publish_digest: tech log carries log_extra")
check(res.get("record", {}).get("recorded") is True, "publish_digest: record.recorded True")

st = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(st["queued_for_digest"] == [], "publish_digest: queue cleared on disk")
check(st["last_digest_run"] == res["record"]["ts"], "publish_digest: last_digest_run set on disk")
check(len(st["digest_runs"]) == 1, "publish_digest: digest_runs appended")
check(st["digest_runs"][-1]["discussion_msg_id"] == 11001,
      f"publish_digest: recorded run carries the mock's discussion_msg_id (got {st['digest_runs'][-1]['discussion_msg_id']})")

# -- scenario 2: comment fails (no linked discussion group) --
mcp_server._save_run_cache({
    "stats": {"articles_in_window": 9, "clusters_in_window": 5, "sources_active": 8,
              "fetch_errors": [], "dead_sources": []},
    "alerted_count": 0, "queued_count": 0, "anti_repeat_matched_clusters": [],
}, reset=True)
calls = []
mcp_server._publish_telegraph_html = lambda title, html: (
    calls.append({"kind": "telegraph"}) or {"url": "https://telegra.ph/y", "missing_placeholders": []})
mcp_server._send_telegram_text = make_text_mock(calls, start_id=12000)
mcp_server._send_telegram_comment = make_comment_mock(calls, error="no linked discussion group")

res2 = publish_digest(baltic=baltic, world=world, tech=tech,
                      methodology="🤖 methodology 2", topics=topics, notes="n2")
check(res2.get("comment", {}).get("error") == "no linked discussion group",
      f"publish_digest (comment fails): comment.error surfaced (got {res2.get('comment')})")
check(res2.get("record", {}).get("recorded") is True,
      "publish_digest (comment fails): run recorded anyway")
tech_log_text2 = [c["text"] for c in calls if c["kind"] == "text" and c["target"] == "log"][0]
check("не отправлен" in tech_log_text2 and "no linked discussion group" in tech_log_text2,
      f"publish_digest (comment fails): tech log mentions the failed comment (got {tech_log_text2!r})")

# -- scenario 3: no methodology/topics -> response as before --
calls = []
mcp_server._publish_telegraph_html = lambda title, html: (
    calls.append({"kind": "telegraph"}) or {"url": "https://telegra.ph/z", "missing_placeholders": []})
mcp_server._send_telegram_text = make_text_mock(calls, start_id=13000)
res3 = publish_digest(baltic=baltic, world=world, tech=tech)
check("comment" not in res3 and "record" not in res3,
      f"publish_digest (no methodology/topics): no comment/record keys (got {sorted(res3.keys())})")
check(res3.get("url") == "https://telegra.ph/z", "publish_digest (no methodology/topics): url still returned")


print()
print("FAILURES:", len(failures))
for f in failures:
    print(" -", f)
sys.exit(1 if failures else 0)
