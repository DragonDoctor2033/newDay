"""Tests for the server-side digest pipeline tools:
digest_context / publish_digest(dry_run) / record_digest_run.

Runs against a throwaway NEWS_ROOT (set BEFORE import) so the real
state.json / inbox / clusters are never touched. Exercises the raw
functions behind the FastMCP tool objects.

    D:\\newDay\\.venv\\Scripts\\python.exe scripts\\test_digest_tools.py
"""
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

tmp = Path(tempfile.mkdtemp(prefix="newday_digest_test_"))
os.environ["NEWS_ROOT"] = str(tmp)

sys.path.insert(0, r"D:\newDay")
import mcp_server  # noqa: E402  (must come after NEWS_ROOT is set)

assert mcp_server.ROOT == tmp, f"NEWS_ROOT not honored: {mcp_server.ROOT}"


def fn(tool):
    return getattr(tool, "fn", tool)


digest_context = fn(mcp_server.digest_context)
digest_baltic_extra = fn(mcp_server.digest_baltic_extra)
publish_digest = fn(mcp_server.publish_digest)
record_digest_run = fn(mcp_server.record_digest_run)
update_state = fn(mcp_server.update_state)
send_telegram = fn(mcp_server.send_telegram)
publish_telegraph = fn(mcp_server.publish_telegraph)

now = datetime.now(timezone.utc)
iso = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
iso_off = lambda dt: dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")  # noqa: E731

# ---------- seed data ----------
def art(i, source, title, hours_ago):
    return {"id": f"art_{i:08x}", "source": source, "title": title,
            "summary": "s", "published": iso_off(now - timedelta(hours=hours_ago)),
            "url": f"https://example.org/{i}"}

articles = [
    art(1, "TASS", "Big world event", 3),
    art(2, "BBC World", "Big world event (BBC)", 2),
    art(3, "Guardian World", "Big world event (Guardian)", 2),
    art(4, "ERR est", "Kohalik uudis", 5),
    art(5, "Postimees RU", "Локальная новость", 4),
    art(6, "ERR News", "Repeat story continues", 6),
    art(7, "Meduza", "Repeat story continues (ru)", 6),
    art(8, "RIA", "Twin of repeat", 7),
    art(9, "ERR est", "Singleton local", 1),
    art(10, "TASS", "Old article outside window", 40),
]
(tmp / "inbox.json").write_text(json.dumps({
    "generated_at": iso(now), "articles": articles,
    "counts_by_source": {}}), encoding="utf-8")

def cl(seed, members, storyline=None, related=()):
    arts = [a for a in articles if a["id"] in members]
    sources = sorted({a["source"] for a in arts})
    outlets = sorted({{"ERR est": "ERR", "ERR News": "ERR", "Postimees RU": "Postimees"}.get(s, s)
                      for s in sources})
    return {"id": seed, "size": len(arts), "sources": sources, "outlets": outlets,
            "langs": ["ru"] if seed == A(8) else ["en"], "first_ts": arts[0]["published"], "last_ts": arts[-1]["published"],
            "title": arts[0]["title"], "sample_ids": [a["id"] for a in arts][:3],
            "article_ids": [a["id"] for a in arts], "storyline_id": storyline,
            "related_ids": list(related)}

A = lambda i: f"art_{i:08x}"  # noqa: E731
clusters = [
    cl(A(1), [A(1), A(2), A(3)]),                       # top world, 3 outlets
    cl(A(4), [A(4), A(5)]),                              # Baltic, 2 outlets
    cl(A(6), [A(6), A(7)]),                              # repeat (direct)
    cl(A(8), [A(8)], related=[A(6)]),                    # repeat via related
    cl(A(9), [A(9)]),                                    # Baltic singleton
    cl(A(10), [A(10)]),                                  # outside window
]
(tmp / "clusters.json").write_text(json.dumps({
    "generated_at": iso(now), "article_count": 10, "cluster_count": len(clusters),
    "multi_outlet_count": 3, "clusters": clusters}), encoding="utf-8")

state = {
    "alerted": [
        {"ts": iso(now - timedelta(hours=2)), "topic_key": "fresh_alert", "title": "Fresh"},
        {"ts": iso(now - timedelta(hours=30)), "topic_key": "stale_alert", "title": "Stale"},
    ],
    "queued_for_digest": [{"id": A(2)}],
    "alerts_today": {"date": "", "count": 0},
    "last_digest_run": iso(now - timedelta(days=1)),
    "digest_runs": [
        {"ts": iso(now - timedelta(days=2)), "topics": [
            {"slug": "repeat_story", "section": "world", "headline": "Repeat story",
             "entities": ["X"], "keywords": ["k"], "article_ids": [A(6)],
             "cluster_ids": [], "depth": "deep_dive",
             "first_seen_run_ts": iso(now - timedelta(days=4)), "appearances": 2,
             "last_substantive_update": None, "live_event": False}]},
        {"ts": iso(now - timedelta(days=1)), "topics": [
            {"slug": "repeat_story", "section": "world", "headline": "Repeat story, day 2",
             "entities": ["X", "Y"], "keywords": ["k"], "article_ids": [A(7)],
             "cluster_ids": [], "depth": "follow_up",
             "first_seen_run_ts": iso(now - timedelta(days=4)), "appearances": 3,
             "last_substantive_update": "new fact", "live_event": True}]},
        {"ts": iso(now - timedelta(days=5)), "topics": [
            {"slug": "ancient", "section": "tech", "headline": "Ancient",
             "article_ids": [A(1)], "depth": "deep_dive", "appearances": 1}]},
    ],
}
(tmp / "state.json").write_text(json.dumps(state), encoding="utf-8")
(tmp / "config.json").write_text(json.dumps({"test_mode": True}), encoding="utf-8")
(tmp / "fetch_errors.json").write_text(json.dumps(
    {"dead_sources": ["The Baltic Times"], "errors": ["x"]}), encoding="utf-8")

failures = []
def check(cond, msg):
    if not cond:
        failures.append(msg)
        print("FAIL:", msg)
    else:
        print("ok:  ", msg)

# ---------- digest_context ----------
ctx = digest_context(hours=24, top=2)
check("error" not in ctx, "digest_context returns no error")
st = ctx["stats"]
check(st["articles_in_window"] == 9, f"articles_in_window == 9 (got {st['articles_in_window']})")
check(st["clusters_in_window"] == 5, f"clusters_in_window == 5 (got {st['clusters_in_window']})")
check(st["multi_outlet_clusters"] == 3, "multi_outlet_clusters == 3")
check(st["dead_sources"] == ["The Baltic Times"], "dead_sources passed through")
top_lines = ctx["top_clusters"].splitlines()
check(len(top_lines) == 2, "top=2 gives two lines")
check(top_lines[0].startswith(A(1)) and " B " not in top_lines[0], "top line 1 = world cluster, no B flag")
check(A(4) in top_lines[1] and " B " in top_lines[1], "top line 2 = Baltic cluster with B flag")
check(f"s:{A(1)},{A(2)},{A(3)}" in top_lines[0], "sample ids in full line")
check("baltic_extra" not in ctx and ctx["baltic_extra_total"] == 2, "context carries only the extra count")
extra = digest_baltic_extra(limit=10)
extra_lines = extra["clusters"].splitlines()
check([l.split()[0] for l in extra_lines] == [A(6), A(9)] and extra["total"] == 2,
      f"digest_baltic_extra = Baltic clusters outside top, largest first (got {extra_lines})")
check("↻repeat_story" in extra_lines[0], "extra lines carry repeat markers")
check(ctx["anti_repeat"]["runs"] == 2 and ctx["anti_repeat"]["topics"] == 1, "anti-repeat: 2 runs / 1 slug in 72h (ancient excluded)")
idx = [l for l in ctx["anti_repeat"]["index"].splitlines() if l.startswith("repeat_story ")][0]
check("app=3" in idx and " LIVE" in idx and "follow_up" in idx and "Repeat story, day 2" in idx and "new: new fact" in idx,
      f"index line takes newest descriptive fields, max appearances ({idx})")
_c = json.loads((tmp / "digest_run_cache.json").read_text(encoding="utf-8"))
check(_c["anti_repeat_index"]["repeat_story"]["first_seen_run_ts"] == iso(now - timedelta(days=4)), "first_seen_run_ts = earliest (kept server-side in run cache)")
check(ctx["anti_repeat"]["topics"] == 1 and "ancient" not in ctx["anti_repeat"]["index"], "index has one line, ancient topic excluded")
check(ctx["anti_repeat"]["clusters_direct"] == 1 and ctx["anti_repeat"]["clusters_related"] == 1,
      "one direct + one related repeat cluster")
# markers are on clusters outside top too — check via a wider call
ctx_wide = digest_context(hours=24, top=10)
lines = {l.split()[0]: l for l in ctx_wide["top_clusters"].splitlines()}
check("↻repeat_story" in lines[A(6)], "direct repeat marker ↻slug")
check("~repeat_story" in lines[A(8)], "related repeat marker ~slug")
check(len(ctx["alerted_24h"]) == 1 and ctx["alerted_24h"][0]["topic_key"] == "fresh_alert", "alerted_24h filtered to window")
_q = ctx["queued_for_digest"]
check(len(_q) == 1 and _q[0]["id"] == A(2) and _q[0]["cluster_id"] == A(1) and _q[0]["status"] == "live"
      and ctx["queued_expired"] == 0,
      f"queued item resolved to its live cluster (got {_q})")
_s = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
_s["queued_for_digest"].append({"id": "art_deadbeef", "topic": "gone"})
(tmp / "state.json").write_text(json.dumps(_s), encoding="utf-8")
_ctx2 = digest_context(hours=24, top=2)
check(_ctx2["queued_expired"] == 1 and _ctx2["queued_for_digest"][-1]["status"] == "expired"
      and _ctx2["queued_for_digest"][-1]["cluster_id"] is None, "queued item outside the map marked expired")
(tmp / "state.json").write_text(json.dumps(state), encoding="utf-8")
ctx = digest_context(hours=24, top=2)
cache = json.loads((tmp / "digest_run_cache.json").read_text(encoding="utf-8"))
check(cache["anti_repeat_index"]["repeat_story"]["appearances"] == 3, "run cache holds anti-repeat counters")

# ---------- publish_digest dry run ----------
baltic = [{"headline": "Local story", "toc": "Local <story>", "essence": "Essence [[%s]]" % A(4),
           "sides": [{"label": "ERR", "text": "a"}, {"label": "Postimees", "text": "b"}],
           "averaged": "avg", "sources": [A(4), A(5)]},
          {"headline": "Single", "essence": "Only one", "note": "пока освещает только ERR", "sources": [A(9)]}]
world = [{"headline": "World", "toc": "World short", "text": "txt", "delta": "⚔ Факты расходятся", "sources": [A(1), A(2)]}]
tech = [{"headline": "Tech", "text": "t", "sources": [A(3)]}]
fu = [{"headline": "Repeat", "whats_new": "nothing", "sources": [A(6)]}]
dry = publish_digest(baltic=baltic, world=world, tech=tech, baltic_followups=fu,
                     world_followups=[], alert_topics=["fresh alert"], dry_run=True)
html = dry["html"]
check(dry["title"].startswith("Дайджест "), "default title")
check("Обработано 9 статей в 5 событиях · " in html, "header stats from run cache")
check("За сутки было 1 срочных алертов:</b> fresh alert." in html, "alert line rendered")
check('<h3 id="Baltic">' in html and '<h3 id="World">' in html and '<h3 id="Tech">' in html, "three anchored sections")
check("<li>Local &lt;story&gt;</li>" in html and "<li>World short</li>" in html and "<li>Tech</li>" in html,
      "TOC uses toc, falls back to headline")
check("<h4>↻ Продолжение сюжетов</h4>" in html and "↻ Продолжение сюжетов мира" not in html,
      "follow-up heading only for non-empty lists")
check("<p><b>Усреднённое.</b> avg</p>" in html and "<li>ERR: a</li>" in html, "deep-dive sections")
check("<p><i>пока освещает только ERR</i></p>" in html, "single-source note")
check("<p>txt Источники: [[%s]] · [[%s]]</p>" % (A(1), A(2)) in html and "<p>⚔ Факты расходятся</p>" in html, "brief item + delta")
check(dry["missing_placeholders"] == [], "all placeholders resolve")
check('<a href="https://example.org/4">ERR est</a>' in dry["html_resolved"], "placeholder resolved to source link")
check("• Local &lt;story&gt;" in dry["main_post"] and '<a href="https://telegra.ph/DRY-RUN">' in dry["main_post"],
      "main post: html-escaped toc bullets + link")
try:
    from telegraph.utils import html_to_nodes
    html_to_nodes(mcp_server._inject_anchors(dry["html_resolved"]))
    check(True, "Telegraph accepts the rendered HTML")
except Exception as e:  # noqa: BLE001
    check(False, f"Telegraph html_to_nodes failed: {e}")
check((tmp / "state.json").read_text(encoding="utf-8") == json.dumps(state), "dry run leaves state untouched")

# publish without tokens must fail cleanly, not send
res = publish_digest(baltic=baltic, world=world, tech=tech)
check(res.get("error", "").startswith("telegraph_token missing"), "publish without token -> error")

# ---------- record_digest_run ----------
mcp_server._save_run_cache({"page_url": "https://telegra.ph/x", "main_msg_id": 188,
                            "discussion_msg_id": 328})
bad = record_digest_run(topics=[{"slug": "no_section"}], notes="x")
check("error" in bad and json.loads((tmp / "state.json").read_text(encoding="utf-8")) == state,
      "invalid topic -> error, state untouched")
topics = [
    {"slug": "repeat_story", "section": "world", "headline": "Repeat again", "entities": ["X"],
     "keywords": ["k"], "article_ids": [A(6)], "depth": "follow_up", "live_event": True},
    {"slug": "local_story", "section": "baltic", "headline": "Local", "entities": ["E"],
     "article_ids": [A(4), A(5)], "cluster_ids": [A(4)], "depth": "deep_dive"},
    {"slug": "single", "section": "baltic", "headline": "Single", "article_ids": [A(9)], "depth": "deep_dive"},
    {"slug": "tech_x", "section": "tech", "headline": "Tech", "article_ids": [A(3)], "depth": "deep_dive"},
]
rec = record_digest_run(topics=topics, notes="test run")
check(rec.get("recorded") is True, "record_digest_run ok")
new_state = json.loads((tmp / "state.json").read_text(encoding="utf-8"))
check(new_state["queued_for_digest"] == [], "queued cleared")
check(new_state["last_digest_run"] == rec["ts"], "last_digest_run set")
check(len(new_state["alerted"]) == 1 and new_state["alerted"][0]["topic_key"] == "fresh_alert", "stale alerted pruned")
runs = new_state["digest_runs"]
check(len(runs) == 4 and runs[-1]["ts"] == rec["ts"], "run appended (10-day window keeps the 5-day-old one)")
run = runs[-1]
check(run["page_url"] == "https://telegra.ph/x" and run["main_msg_id"] == 188 and run["discussion_msg_id"] == 328,
      "publish ids from run cache")
check(run["articles_processed"] == 9 and run["sources_in_window"] == 8, "stats from run cache")
check(run["baltic_deep_dives"] == 2 and run["world_items"] == 0 and run["world_followups"] == 1 and run["tech_items"] == 1,
      "section counts derived from topics")
check(run["alerted_summary_count"] == 1 and run["queued_items_consumed_approx"] == 1 and run["test_mode"] is True,
      "alert/queue counters + test_mode")
t0 = run["topics"][0]
check(t0["appearances"] == 4 and t0["first_seen_run_ts"] == iso(now - timedelta(days=4)),
      "repeat topic: appearances = index+1, first_seen kept")
t1 = run["topics"][1]
check(t1["appearances"] == 1 and t1["first_seen_run_ts"] == rec["ts"] and t1["keywords"] == [],
      "new topic: appearances 1, first_seen = now, defaults filled")
check(rec["digest_runs_count"] == 4, "digest_runs_count reported")

# ---------- verify_card / read_full ----------
verify_card = fn(mcp_server.verify_card)
read_full = fn(mcp_server.read_full)
(tmp / "cache").mkdir(exist_ok=True)
(tmp / "cache" / f"{A(4)}.txt").write_text(
    "Minister ütles: «Lennuühendus ei sõltu ühest firmast». Tallinnast jääb 25 sihtkohta, "
    "kaob 11 liini — 4% reisijatest. Hind 12,5 eurot.", encoding="utf-8")
(tmp / "cache" / f"{A(5)}.txt").write_text("Другой текст: 24 направления останутся.", encoding="utf-8")
card = {
    "cluster_id": A(4), "headline_ru": "x",
    "quotes": [
        {"text": "Lennuühendus ei sõltu ühest firmast", "who": "minister", "article_id": A(4)},
        {"text": "\u201eLennuühendus  ei sõltu\u201c ... ühest firmast", "who": "minister", "article_id": A(4)},
        {"text": "Продавать ведь не запрещают", "who": "seller", "article_id": A(4)},
        {"text": "anything", "who": "x", "article_id": "art_deadbeef"},
    ],
    "numbers": [
        {"value": "25", "meaning": "destinations", "article_id": A(4)},
        {"value": "4%", "meaning": "share", "article_id": A(4)},
        {"value": "12.5 евро", "meaning": "price", "article_id": A(4)},
        {"value": "24", "meaning": "computed 13+11", "article_id": A(4)},
        {"value": "24", "meaning": "in the other article", "article_id": A(5)},
    ],
    "disagreements": [
        {"claim": "25 vs 24", "evidence": [
            {"article_id": A(4), "quote": "jääb 25 sihtkohta"},
            {"article_id": A(5), "quote": "24 направления останутся"}]},
        {"claim": "fabricated", "evidence": [
            {"article_id": A(4), "quote": "jääb 25 sihtkohta"},
            {"article_id": A(5), "quote": "24 направления закроют"}]},
        "plain string disagreement",
    ],
    "notes": "n",
}
v = verify_card(card)
check(v["kept"] == {"quotes": 2, "numbers": 4, "disagreements": 1}, f"verify_card keeps only verifiable items (got {v['kept']})")
check([q["text"] for q in v["dropped"]["quotes"]] == ["Продавать ведь не запрещают", "anything"], "invented / unknown-article quotes dropped")
check([n["meaning"] for n in v["dropped"]["numbers"]] == ["computed 13+11"], "computed number dropped, others verified incl. 4% and 12,5")
check([d["claim"] for d in v["dropped"]["disagreements"]] == ["fabricated", "plain string disagreement"], "disagreement needs 2 verified evidence quotes")
check(v["articles_missing"] == ["art_deadbeef"], "missing article reported")
check(v["card"]["headline_ru"] == "x" and v["card"]["notes"] == "n", "other fields pass through")
rf = read_full(A(4), max_chars=20)
check(rf["truncated"] is True and len(rf["text"]) == 20 and rf["cached"] is True and rf["total_chars"] > 20, "read_full honours max_chars and reports truncation")
rf = read_full(A(4))
check(rf["truncated"] is False, "read_full default returns the whole cached text")

# ---------- refactored tools still work ----------
r = update_state({"append_queued": [{"id": "q1"}]})
check(r.get("queued_for_digest") == [{"id": "q1"}] and "digest_runs_count" in r, "update_state wrapper works")
check(send_telegram("hi").get("error", "").startswith("tg_bot_token"), "send_telegram wrapper works")
check(publish_telegraph("t", "<p>x</p>").get("error", "").startswith("telegraph_token"), "publish_telegraph wrapper works")

print()
print("FAILURES:", len(failures))
for f in failures:
    print(" -", f)
sys.exit(1 if failures else 0)
