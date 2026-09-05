"""
News MCP server.

Exposes a small set of tools that let the LLM:
  - browse the inbox of fresh articles by id (no URLs leak through),
  - read full article text on demand,
  - cite articles by id (we render the actual <a href>),
  - publish to Telegraph and Telegram with placeholder substitution,
  - manage state.json atomically,
  - run the daily digest pipeline with the mechanics server-side
    (digest_context / publish_digest / record_digest_run),
  - feed a reader sub-agent one event in one call (reader_packet),
  - run the hourly watchman in two calls (watchman_context /
    watchman_finish).

The whole point: the LLM never types URLs by hand. Placeholders like
[[art_a3f9]] in HTML/text are replaced server-side using inbox data.

Run as stdio MCP server (Claude Code launches it via .mcp.json).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from fastmcp import FastMCP

from doh import install_global_doh

# Resolve every outbound hostname via private DoH instead of the system
# resolver, so ISP-level DNS blocks (Estonia: ria/tass/lrt/yle/err/postimees)
# don't apply to read_full() or to the Telegraph/Telegram API calls.
install_global_doh()


ROOT = Path(os.environ.get("NEWS_ROOT", Path(__file__).parent)).resolve()
INBOX_PATH = ROOT / "inbox.json"
CLUSTERS_PATH = ROOT / "clusters.json"
STATE_PATH = ROOT / "state.json"
CONFIG_PATH = ROOT / "config.json"
CACHE_DIR = ROOT / "cache"
CACHE_DIR.mkdir(exist_ok=True)

# digest_runs retention, days. Anti-repeat (digest ШАГ 1.5) reads only 72h
# back; feedback_collector matches user replies to runs for up to ~a week
# (weekly review cadence). 10 days covers both while keeping state.json and
# get_state payloads bounded.
DIGEST_RUNS_KEEP_DAYS = 10

PLACEHOLDER_RE = re.compile(r"\[\[(art_[0-9a-f]{8})\]\]")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

mcp = FastMCP("news-agent")


# ────────────────────── Internal helpers ──────────────────────

def _load_inbox() -> dict[str, Any]:
    if not INBOX_PATH.exists():
        return {"articles": []}
    return json.loads(INBOX_PATH.read_text(encoding="utf-8"))


def _index_inbox() -> dict[str, dict]:
    return {a["id"]: a for a in _load_inbox().get("articles", [])}


def _load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {
            "alerted": [],
            "queued_for_digest": [],
            "last_watchman_run": None,
            "last_digest_run": None,
            "alerts_today": {"date": "", "count": 0},
        }
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def _write_state(state: dict[str, Any]) -> None:
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_PATH)


def _resolve_placeholders(text: str, *, html: bool) -> tuple[str, list[str]]:
    """Replace [[art_xxx]] placeholders with real links. Returns (text, missing_ids)."""
    inbox = _index_inbox()
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        art_id = m.group(1)
        art = inbox.get(art_id)
        if not art:
            missing.append(art_id)
            return f"[?{art_id}]"
        if html:
            return f'<a href="{art["url"]}">{art["source"]}</a>'
        return f'[{art["source"]}]({art["url"]})'

    return PLACEHOLDER_RE.sub(repl, text), missing


# ────────────────────── Tools: news access ──────────────────────

@mcp.tool()
def list_news(
    since_iso: str | None = None,
    source_filter: str | None = None,
    limit: int = 100,
    before_iso: str | None = None,
    compact: bool = False,
    max_per_source: int | None = None,
) -> dict:
    """
    List articles currently in the inbox, newest first.

    URLs are NOT returned. To cite an article, use cite() with its id.

    Args:
        since_iso: only articles with `published >= since_iso` (UTC ISO).
        source_filter: case-insensitive substring match on source name.
        limit: max number of articles to return.
        before_iso: only articles with `published < before_iso` (UTC ISO).
            Pagination cursor — pass `next_before_iso` from the previous
            response to fetch the next (older) page. Time-based, so pages
            stay stable even while the fetcher rewrites the inbox.
        compact: drop `summary` from each item (~2x smaller response).
            Use for wide sweeps; pull details per article via read_full
            or per source via a non-compact source_filter call.
        max_per_source: keep at most N newest articles per source within
            the window. Evens out flood sources (TASS/RIA are >50% of
            the feed) so quieter regional sources aren't crowded out.

    Returns:
        count            — articles in this response
        window_total     — articles matching the filters BEFORE `limit`;
                           if window_total > count the window is truncated
        truncated        — bool, see above
        next_before_iso  — cursor for the next page (null unless truncated)
        total_in_inbox   — inbox size ignoring filters
        articles         — id/source/title/published (+summary unless compact)

    A full 24h sweep that usually fits in one call:
    list_news(since_iso=<now-24h>, limit=900, compact=True, max_per_source=50).
    """
    all_articles = _load_inbox().get("articles", [])
    articles = all_articles
    if since_iso:
        articles = [a for a in articles if a["published"] >= since_iso]
    if before_iso:
        articles = [a for a in articles if a["published"] < before_iso]
    if source_filter:
        sf = source_filter.lower()
        articles = [a for a in articles if sf in a["source"].lower()]

    # The fetcher keeps the inbox newest-first, but don't rely on it.
    articles = sorted(articles, key=lambda a: a["published"], reverse=True)

    if max_per_source is not None:
        per_source: dict[str, int] = {}
        balanced = []
        for a in articles:
            n = per_source.get(a["source"], 0)
            if n < max_per_source:
                per_source[a["source"]] = n + 1
                balanced.append(a)
        articles = balanced

    window_total = len(articles)
    page = articles[:limit]
    # Never split a page inside a same-second run: the `< next_before_iso`
    # cursor would silently skip the equal-timestamp leftovers.
    while 0 < len(page) < window_total and (
        articles[len(page)]["published"] == page[-1]["published"]
    ):
        page.append(articles[len(page)])
    truncated = window_total > len(page)

    fields = (
        ("id", "source", "title", "published")
        if compact
        else ("id", "source", "title", "summary", "published")
    )
    return {
        "count": len(page),
        "window_total": window_total,
        "truncated": truncated,
        "next_before_iso": page[-1]["published"] if truncated else None,
        "total_in_inbox": len(all_articles),
        "articles": [{k: a[k] for k in fields} for a in page],
    }


def _load_clusters() -> dict[str, Any]:
    if not CLUSTERS_PATH.exists():
        return {}
    return json.loads(CLUSTERS_PATH.read_text(encoding="utf-8"))


CLUSTERS_SNAPSHOT_PATH = ROOT / "digest_clusters_snapshot.json"


def _find_cluster(cluster_id: str, data: dict[str, Any] | None = None
                  ) -> tuple[dict | None, str]:
    """Resolve a cluster id the model got earlier in the run.

    clusterer.py rewrites clusters.json every fetch cycle (~15 min), and a
    cluster id is only stable while its seed article stays the seed; a
    merge or re-split renames it. Order: exact id in the live file → the
    live cluster that CONTAINS that article id (the story went on under a
    new seed) → the snapshot digest_context saved at run start. Returns
    (cluster, how) with how in {"live", "member", "snapshot", ""}.
    """
    data = data if data is not None else _load_clusters()
    clusters = data.get("clusters", []) if data else []
    for c in clusters:
        if c.get("id") == cluster_id:
            return c, "live"
    for c in clusters:
        if cluster_id in (c.get("article_ids") or []):
            return c, "member"
    if CLUSTERS_SNAPSHOT_PATH.exists():
        try:
            snap = json.loads(CLUSTERS_SNAPSHOT_PATH.read_text(encoding="utf-8"))
        except ValueError:
            snap = {}
        for c in snap.get("clusters", []):
            if c.get("id") == cluster_id:
                return c, "snapshot"
    return None, ""


@mcp.tool()
def list_clusters(
    since_iso: str | None = None,
    min_outlets: int = 1,
    limit: int = 100,
) -> dict:
    """
    List event clusters built by the deterministic clusterer (clusterer.py
    runs after each fetch; embeddings + cosine similarity, no LLM).

    A cluster = one real-world event as covered by 1..N sources across
    languages. Use this INSTEAD of sweeping list_news when you need the map
    of what happened: 3000 articles/day collapse into event clusters, most
    coverage-worthy first.

    Args:
        since_iso: only clusters with activity after this time
            (`last_ts >= since_iso`, UTC ISO).
        min_outlets: only clusters covered by >= N independent newsrooms.
            NOTE: `outlets` counts newsrooms, not feeds — Pravda RU + Pravda
            UA is ONE outlet (same newsroom, two languages), same for
            ERR / Postimees language editions. Use outlets, not sources,
            for the "confirmed by >=2 independent sources" check.
        limit: max clusters returned.

    Returns clusters sorted by coverage (outlet count, then size):
        id           — cluster id (= id of its seed article, stable while
                       the seed stays in the 72h window)
        title        — seed article title (representative headline)
        size         — number of articles
        outlets      — independent newsrooms covering the event
        langs        — language zones covering it (en/ru/et/uk)
        first_ts / last_ts — activity window
        sample_ids   — up to 3 citation-ready article ids from DISTINCT
                       outlets. ALWAYS cite from these (2-3 ids) for any
                       published item — the bare cluster id is just the
                       EARLIEST article (usually a newswire like TASS),
                       citing it alone over-credits the fastest source.
        storyline_id — non-null if this event was split out of a bigger
                       storyline blob (e.g. daily war coverage); events
                       sharing a storyline_id belong to the same ongoing
                       storyline. Singleton leftovers of such blobs are
                       routine background noise.
        related_count — number of related clusters (twins/neighbours).
                       en<->ru versions of one event often land in two
                       monolingual clusters — before a deep-dive, call
                       get_cluster and check its related_ids for the
                       other language's take; treat the union as one
                       event. The ids themselves are only returned by
                       get_cluster to keep this listing small.
    Full article lists: get_cluster(id).
    """
    data = _load_clusters()
    if not data:
        return {"error": "clusters.json not found — clusterer has not run yet"}
    clusters = data.get("clusters", [])
    if since_iso:
        clusters = [c for c in clusters if (c.get("last_ts") or "") >= since_iso]
    if min_outlets > 1:
        clusters = [c for c in clusters if len(c.get("outlets", [])) >= min_outlets]
    window_total = len(clusters)
    page = clusters[:limit]
    fields = ("id", "title", "size", "outlets", "langs",
              "first_ts", "last_ts", "storyline_id", "sample_ids")
    return {
        "generated_at": data.get("generated_at"),
        "count": len(page),
        "window_total": window_total,
        "truncated": window_total > len(page),
        "clusters": [
            {**{k: c.get(k) for k in fields},
             "related_count": len(c.get("related_ids", []))}
            for c in page
        ],
    }


@mcp.tool()
def get_cluster(cluster_id: str) -> dict:
    """
    Full contents of one event cluster: every article (id/source/title/
    summary/published) joined against the inbox, grouped for narrative
    comparison. Articles that already left the 72h inbox window are
    listed in `expired_ids` (no longer citable).

    Typical use: pick clusters via list_clusters, then get_cluster on the
    ones worth a deep-dive — you get all versions of the same event from
    different sources/languages side by side (the delta is the signal:
    what facts differ, what each side emphasises, who stays silent).
    """
    data = _load_clusters()
    if not data:
        return {"error": "clusters.json not found — clusterer has not run yet"}
    cluster, how = _find_cluster(cluster_id, data)
    if cluster is None:
        return {"error": f"unknown cluster id: {cluster_id} — not in the live "
                         "map, not a member of any live cluster, not in the "
                         "digest snapshot"}
    inbox = _index_inbox()
    articles, expired = [], []
    for aid in cluster.get("article_ids", []):
        art = inbox.get(aid)
        if art:
            articles.append({k: art[k] for k in
                             ("id", "source", "title", "summary", "published")})
        else:
            expired.append(aid)
    return {
        **{k: cluster.get(k) for k in
           ("id", "title", "size", "sources", "outlets", "langs",
            "first_ts", "last_ts", "storyline_id", "related_ids")},
        "articles": articles,
        "expired_ids": expired,
        "resolved_via": how,
    }


READ_FULL_DEFAULT_CHARS = 8000
READ_FULL_CACHE_CHARS = 60000   # cache keeps the whole extraction; callers cut
_LEGACY_CACHE_CHARS = 8000      # caches written before 02.09.2026 were cut here


def _fetch_article_text(art: dict) -> str | dict:
    """Fetch + extract an article body. Returns text, or {"error": ...}."""
    try:
        r = requests.get(
            art["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=20,
        )
    except requests.RequestException as e:
        return {"error": str(e)}
    if r.status_code != 200:
        return {"error": f"HTTP {r.status_code} fetching article"}
    # very rough text extraction; for production swap in trafilatura/readability
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))[:READ_FULL_CACHE_CHARS]


def _article_text(article_id: str, *, allow_fetch: bool = True,
                  want_chars: int = READ_FULL_DEFAULT_CHARS) -> tuple[str | None, bool, str | None]:
    """(text, from_cache, error). Re-fetches a legacy 8000-char cache entry
    when the caller wants more than that."""
    inbox = _index_inbox()
    art = inbox.get(article_id)
    if not art:
        return None, False, f"unknown article id: {article_id}"
    cache_file = CACHE_DIR / f"{article_id}.txt"
    if cache_file.exists():
        text = cache_file.read_text(encoding="utf-8")
        if not text.strip():
            text = ""          # empty file (blocked fetch) → treat as not cached
        elif not (len(text) == _LEGACY_CACHE_CHARS and want_chars > _LEGACY_CACHE_CHARS
                  and allow_fetch):
            return text, True, None
    if not allow_fetch:
        return None, False, "not cached"
    got = _fetch_article_text(art)
    if isinstance(got, dict):
        return None, False, got["error"]
    cache_file.write_text(got, encoding="utf-8")
    return got, False, None


@mcp.tool()
def read_full(article_id: str, max_chars: int = READ_FULL_DEFAULT_CHARS) -> dict:
    """
    Fetch full article text for a given inbox id. Cached to disk.

    Returns plain text (HTML stripped). The URL is fetched server-side and
    not exposed to the model — only the resulting text body.

    max_chars: how much of the text to return (default 8000). Long reads
    (Meduza, FT long-form) get cut at the default — pass e.g. 20000 when
    you need the whole piece; `truncated` tells you whether more exists.
    """
    inbox = _index_inbox()
    art = inbox.get(article_id)
    if not art:
        return {"error": f"unknown article id: {article_id}"}
    text, cached, err = _article_text(article_id, want_chars=max_chars)
    if err:
        return {"error": err}
    return {
        "id": article_id,
        "source": art["source"],
        "title": art["title"],
        "text": text[:max_chars],
        "truncated": len(text) > max_chars,
        "total_chars": len(text),
        "cached": cached,
    }


@mcp.tool()
def cite(article_ids: list[str], format: str = "html") -> dict:
    """
    Render a list of article ids as ready-to-use citation links.

    Args:
        article_ids: list of inbox ids.
        format: 'html' for <a href>, 'markdown' for [text](url),
                'telegraph_ul' for <ul><li><a/></li></ul>.

    The model never writes URLs by hand — always go through cite().
    """
    inbox = _index_inbox()
    items: list[dict] = []
    missing: list[str] = []

    for aid in article_ids:
        art = inbox.get(aid)
        if not art:
            missing.append(aid)
            continue
        items.append(art)

    if format == "html":
        rendered = " · ".join(f'<a href="{a["url"]}">{a["source"]}</a>' for a in items)
    elif format == "markdown":
        rendered = " · ".join(f'[{a["source"]}]({a["url"]})' for a in items)
    elif format == "telegraph_ul":
        rendered = (
            "<ul>"
            + "".join(f'<li><a href="{a["url"]}">{a["source"]}: {a["title"]}</a></li>' for a in items)
            + "</ul>"
        )
    else:
        return {"error": f"unknown format: {format}"}

    return {"rendered": rendered, "resolved": len(items), "missing": missing}


# ────────────────────── Tools: state ──────────────────────

@mcp.tool()
def get_state(include_digest_runs: bool = False,
              digest_runs_hours: int = 78) -> dict:
    """
    Return the current state.json contents.

    `digest_runs` (per-run topics[] history, ~70KB over 10 days) is omitted
    by default: only the digest's anti-repeat step (digest ШАГ 1.5) reads
    it, while the hourly watchman needs just alerted / queued_for_digest /
    alerts_today / last_watchman_run. Pass include_digest_runs=True to get
    it — trimmed to the last `digest_runs_hours` (default 78h: covers the
    72h anti-repeat window with slack for late runs; pass 0 for the full
    stored history). Read-side filtering only — state.json on disk always
    keeps the full 10 days.
    """
    state = _load_state()
    if not include_digest_runs:
        state.pop("digest_runs", None)
    elif digest_runs_hours and "digest_runs" in state:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(hours=digest_runs_hours)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        state["digest_runs"] = [
            r for r in state["digest_runs"] if str(r.get("ts", "")) >= cutoff
        ]
    return state


@mcp.tool()
def update_state(patch: dict) -> dict:
    """
    Shallow-merge `patch` into state.json and write atomically.

    Special keys:
      - 'append_alerted': list of records appended to state.alerted
      - 'append_queued':  list of records appended to state.queued_for_digest
      - 'append_digest_run': ONE run record (dict) appended to
        state.digest_runs; the server then drops runs older than 10 days.
        Never read back or resend the whole digest_runs array — pass only
        the new record here.
      - 'clear_queued':   bool, if true sets queued_for_digest = []
      - 'increment_alerts_today': int, adds to alerts_today.count for today

    Any other key containing 'append' or starting with 'clear_'/'increment_'
    is rejected and nothing is written — a misspelled special key must fail
    loudly instead of silently becoming a literal state.json field.

    Returns the merged state, with `digest_runs` (per-run topics[] history,
    ~70KB) replaced by a `digest_runs_count` integer: no caller needs the
    array back (watchman never reads it, digest ШАГ 9.3 appends blindly).
    Read-side filtering only — state.json on disk always keeps digest_runs;
    use get_state(include_digest_runs=true) to read it.
    """
    return _apply_state_patch(patch)


def _apply_state_patch(patch: dict) -> dict:
    """Body of update_state — shared with record_digest_run."""
    state = _load_state()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()

    if patch.pop("clear_queued", False):
        state["queued_for_digest"] = []

    if appends := patch.pop("append_alerted", None):
        state["alerted"] = state.get("alerted", []) + list(appends)

    if appends := patch.pop("append_queued", None):
        state["queued_for_digest"] = state.get("queued_for_digest", []) + list(appends)

    if run := patch.pop("append_digest_run", None):
        new_runs = run if isinstance(run, list) else [run]
        for r in new_runs:
            r.setdefault("ts", now.strftime("%Y-%m-%dT%H:%M:%SZ"))
        cutoff = (now - timedelta(days=DIGEST_RUNS_KEEP_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        state["digest_runs"] = [
            r
            for r in state.get("digest_runs", []) + new_runs
            if str(r.get("ts", "")) >= cutoff
        ]

    if inc := patch.pop("increment_alerts_today", None):
        if state.get("alerts_today", {}).get("date") != today:
            state["alerts_today"] = {"date": today, "count": 0}
        state["alerts_today"]["count"] = state["alerts_today"].get("count", 0) + int(inc)

    if bad := [
        k for k in patch if "append" in k or k.startswith(("clear_", "increment_"))
    ]:
        return {
            "error": "unsupported special key(s): "
                     f"{', '.join(sorted(bad))} — state.json was NOT modified",
            "supported_special_keys": [
                "append_alerted", "append_queued", "append_digest_run",
                "clear_queued", "increment_alerts_today",
            ],
        }

    # generic merge for anything else
    state.update(patch)
    _write_state(state)
    if (runs := state.pop("digest_runs", None)) is not None:
        state["digest_runs_count"] = len(runs)
    return state


@mcp.tool()
def cleanup_state(max_age_hours: int = 24) -> dict:
    """Remove `alerted` records older than max_age_hours. Returns updated state."""
    state = _load_state()
    cutoff = datetime.now(timezone.utc).timestamp() - max_age_hours * 3600

    def _is_recent(rec: dict) -> bool:
        try:
            return datetime.fromisoformat(rec.get("ts", "")).timestamp() >= cutoff
        except ValueError:
            return False

    before = len(state.get("alerted", []))
    state["alerted"] = [r for r in state.get("alerted", []) if _is_recent(r)]
    _write_state(state)
    return {"removed": before - len(state["alerted"]), "kept": len(state["alerted"])}


# ────────────────────── Tools: publishing ──────────────────────

@mcp.tool()
def publish_telegraph(title: str, html_with_placeholders: str) -> dict:
    """
    Publish an HTML page to Telegraph.

    Inside `html_with_placeholders`, use [[art_xxx]] anywhere a link should appear.
    The server will replace each placeholder with <a href="...">Source</a>
    using the inbox. URLs typed by hand are NOT processed — only placeholders.

    Anchors:
      You can write <h3 id="Baltic">…</h3> in the input HTML and reference it
      from a TOC as <a href="#Baltic">…</a>. Telegraph drops `id` from headings
      but keeps `id` on <a>. The server rewrites each header-with-id into an
      invisible <a id="…"></a> placed just before the (now plain) header,
      so anchors actually work in the published page.
    """
    return _publish_telegraph_html(title, html_with_placeholders)


def _publish_telegraph_html(title: str, html_with_placeholders: str) -> dict:
    """Body of publish_telegraph — shared with publish_digest."""
    # Debug: dump what we receive from the LLM
    debug_path = ROOT / "logs" / "telegraph_debug.html"
    debug_path.parent.mkdir(exist_ok=True)
    debug_path.write_text(html_with_placeholders, encoding="utf-8")
    
    cfg = _load_config()
    token = cfg.get("telegraph_token")
    if not token:
        return {"error": "telegraph_token missing in config.json"}

    html, missing = _resolve_placeholders(html_with_placeholders, html=True)
    html = _inject_anchors(html)

    # Telegraph API needs Node-format JSON, not raw HTML — convert.
    try:
        from telegraph.utils import html_to_nodes
    except ImportError:
        return {"error": "telegraph package not installed (pip install telegraph)"}

    try:
        nodes = html_to_nodes(html)
    except Exception as e:
        return {
            "error": f"html_to_nodes failed: {e}",
            "hint": "Telegraph only allows: a, aside, b, blockquote, br, code, em, "
                    "figcaption, figure, h3, h4, hr, i, iframe, img, li, ol, p, pre, "
                    "s, strong, u, ul, video.",
        }

    r = requests.post(
        "https://api.telegra.ph/createPage",
        data={
            "access_token": token,
            "title": title,
            "author_name": "Claude News",
            "content": json.dumps(nodes, ensure_ascii=False),
            "return_content": "false",
        },
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        return {"error": data.get("error", "unknown"), "telegraph_response": data}
    return {
        "url": data["result"]["url"],
        "path": data["result"]["path"],
        "missing_placeholders": missing,
    }


_HEADING_WITH_ID = re.compile(
    r'<(h[34])\s+([^>]*?)id=["\']([^"\']+)["\']([^>]*)>(.*?)</\1>',
    re.IGNORECASE | re.DOTALL,
)
_HREF_HASH = re.compile(r'''href=(['"])#([^'"]+)\1''')


def _telegraph_slug(text: str) -> str:
    """
    Reproduce the slug Telegraph generates for h3/h4 ids server-side:
    strip outer whitespace, then collapse any internal whitespace runs
    into single '-'. Everything else (punctuation, emoji, casing) is
    preserved verbatim — that's what Telegraph itself does.
    """
    return re.sub(r"\s+", "-", text.strip())


def _inject_anchors(html: str) -> str:
    """
    Telegraph drops `id` from headings AND drops empty <a id> nodes, so
    custom anchors don't survive. But Telegraph auto-generates an `id` on
    every h3/h4 by slugifying the heading text. So we:
      1. For each <h3 id="X">text</h3>, compute slug(text) and remember
         X -> slug. Strip the dead `id` attribute (Telegraph ignores it).
      2. Rewrite every href="#X" to href="#<slug>", so TOC links land on
         the ids Telegraph itself produces.
    """
    id_to_slug: dict[str, str] = {}

    def repl_heading(m: re.Match) -> str:
        tag, before, anchor_id, after, inner = m.groups()
        plain = re.sub(r"<[^>]+>", "", inner)
        id_to_slug[anchor_id] = _telegraph_slug(plain)
        leftover = (before + after).strip()
        open_tag = f"<{tag}>" if not leftover else f"<{tag} {leftover}>"
        return f"{open_tag}{inner}</{tag}>"

    html = _HEADING_WITH_ID.sub(repl_heading, html)

    def repl_href(m: re.Match) -> str:
        quote, anchor_id = m.group(1), m.group(2)
        slug = id_to_slug.get(anchor_id)
        return f"href={quote}#{slug}{quote}" if slug else m.group(0)

    return _HREF_HASH.sub(repl_href, html)


@mcp.tool()
def send_telegram(
    text_with_placeholders: str,
    target: str = "main",
    parse_mode: str = "Markdown",
    disable_preview: bool = False,
) -> dict:
    """
    Send a Telegram message. target is one of: 'main', 'log', 'test'.

    Uses [[art_xxx]] placeholders → markdown links (or HTML if parse_mode=HTML).
    Auto-splits messages longer than 4000 characters at paragraph boundaries.

    If config has test_mode=true and target='main', sends to test_chat instead
    and prepends '🧪 [TEST] ' to each message.
    """
    return _send_telegram_text(text_with_placeholders, target=target,
                               parse_mode=parse_mode,
                               disable_preview=disable_preview)


def _send_telegram_text(
    text_with_placeholders: str,
    target: str = "main",
    parse_mode: str = "Markdown",
    disable_preview: bool = False,
) -> dict:
    """Body of send_telegram — shared with publish_digest."""
    cfg = _load_config()
    token = cfg.get("tg_bot_token")
    if not token:
        return {"error": "tg_bot_token missing in config.json"}

    # target resolution + test mode
    target_to_field = {
        "main": "tg_chat_id",
        "log": "tg_log_chat_id",
        "test": "tg_test_chat_id",
    }
    if target == "main" and cfg.get("test_mode"):
        chat_id = cfg.get("tg_test_chat_id")
        text_with_placeholders = "🧪 [TEST]\n" + text_with_placeholders
    else:
        chat_id = cfg.get(target_to_field.get(target, ""))
    if not chat_id:
        return {"error": f"chat id for target '{target}' not configured"}

    text, missing = _resolve_placeholders(
        text_with_placeholders, html=(parse_mode == "HTML")
    )

    chunks = _split_text(text, 4000)
    results = []
    for chunk in chunks:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": "true" if disable_preview else "false",
            },
            timeout=20,
        )
        try:
            results.append(r.json())
        except ValueError:
            results.append({"ok": False, "raw": r.text})
        time.sleep(0.4)

    return {
        "sent_chunks": len(chunks),
        "missing_placeholders": missing,
        "results": results,
    }


@mcp.tool()
def send_telegram_comment(
    text_with_placeholders: str,
    channel_msg_id: int,
    parse_mode: str = "Markdown",
) -> dict:
    """
    Post a comment under a channel message in the linked discussion group.

    Requires the channel to be linked to a discussion supergroup, and the
    bot to be admin in both with 'Send messages' permission. The discussion
    group should restrict members so only admins can post — that gives a
    read-only comment thread where only this bot publishes.

    Args:
        text_with_placeholders: same [[art_xxx]] placeholders as send_telegram.
        channel_msg_id: the message_id returned by send_telegram(target="main").
        parse_mode: 'Markdown' or 'HTML'.
    """
    return _send_telegram_comment(text_with_placeholders, channel_msg_id,
                                  parse_mode=parse_mode)


def _send_telegram_comment(
    text_with_placeholders: str,
    channel_msg_id: int,
    parse_mode: str = "Markdown",
) -> dict:
    """Body of send_telegram_comment — shared with publish_digest and
    watchman_finish."""
    cfg = _load_config()
    token = cfg.get("tg_bot_token")
    if not token:
        return {"error": "tg_bot_token missing in config.json"}

    # Mirror send_telegram's test_mode routing — if test_mode is on, the
    # main digest post lives in tg_test_chat_id, so we comment under that
    # channel's linked discussion group, not the production one.
    if cfg.get("test_mode"):
        channel_id = cfg.get("tg_test_chat_id")
        channel_label = "tg_test_chat_id (test_mode on)"
    else:
        channel_id = cfg.get("tg_chat_id")
        channel_label = "tg_chat_id"
    if not channel_id:
        return {"error": f"{channel_label} missing in config.json"}

    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getChat",
            params={"chat_id": channel_id},
            timeout=20,
        )
        info = r.json()
    except (requests.RequestException, ValueError) as e:
        return {"error": f"getChat failed: {e}"}
    if not info.get("ok"):
        return {"error": "getChat returned !ok", "telegram_response": info}

    linked_id = info["result"].get("linked_chat_id")
    if not linked_id:
        return {
            "error": f"{channel_label} channel has no linked discussion "
                     f"group — link one in Telegram channel settings and "
                     f"add the bot as admin with 'Send messages'",
            "channel_id_checked": channel_id,
        }

    # Find the auto-forwarded copy of the channel post in the discussion
    # group. Telegram needs reply_to_message_id pointing at THAT message
    # (not the channel one) for comments to thread under the channel post.
    # Cross-chat reply_parameters creates external_reply instead — visible
    # in the group but not as a comment.
    discussion_msg_id = _find_auto_forward(
        token, linked_id, channel_msg_id, max_wait_s=10
    )
    if discussion_msg_id is None:
        return {
            "error": "could not find auto-forwarded copy of channel post "
                     "in discussion group via getUpdates within 10s — the "
                     "auto-forward may be delayed, or another process is "
                     "consuming updates with offset",
            "channel_msg_id": channel_msg_id,
            "discussion_chat_id": linked_id,
        }

    text, missing = _resolve_placeholders(
        text_with_placeholders, html=(parse_mode == "HTML")
    )

    chunks = _split_text(text, 4000)
    results = []
    first_chunk_msg_id: int | None = None
    for i, chunk in enumerate(chunks):
        payload: dict[str, Any] = {
            "chat_id": linked_id,
            "text": chunk,
            "parse_mode": parse_mode,
            "disable_web_page_preview": "true",
            "reply_parameters": {
                "message_id": (
                    discussion_msg_id if i == 0 else first_chunk_msg_id
                ),
            },
        }
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=20,
        )
        try:
            data = r.json()
            results.append(data)
            if i == 0 and data.get("ok"):
                first_chunk_msg_id = data["result"]["message_id"]
        except ValueError:
            results.append({"ok": False, "raw": r.text})
        time.sleep(0.4)

    if first_chunk_msg_id is not None:
        # remembered for record_digest_run (feedback_collector matches
        # user replies to a run by this id)
        _save_run_cache({"discussion_msg_id": discussion_msg_id})

    return {
        "discussion_chat_id": linked_id,
        "discussion_msg_id": discussion_msg_id,
        "sent_chunks": len(chunks),
        "missing_placeholders": missing,
        "results": results,
    }


def _find_auto_forward(
    token: str,
    linked_id: int,
    channel_msg_id: int,
    max_wait_s: int = 10,
) -> int | None:
    """
    Poll Telegram getUpdates looking for the auto-forwarded copy of
    `channel_msg_id` inside the discussion group `linked_id`.

    Telegram emits one update per auto-forward with is_automatic_forward
    set and forward_origin.message_id pointing at the original channel
    post. We don't acknowledge updates (no offset advancement) so the
    queue stays available for repeated lookups across runs.
    """
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{token}/getUpdates",
                params={"limit": 100, "timeout": 1},
                timeout=5,
            )
            for upd in r.json().get("result", []):
                msg = upd.get("message") or {}
                if (
                    msg.get("chat", {}).get("id") == linked_id
                    and msg.get("is_automatic_forward")
                    and msg.get("forward_origin", {}).get("message_id")
                        == channel_msg_id
                ):
                    return msg["message_id"]
        except requests.RequestException:
            pass
        time.sleep(0.5)
    return None


def _split_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n\n", 0, limit)
        if cut < 1:
            cut = text.rfind("\n", 0, limit)
        if cut < 1:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return chunks


# ────────────────────── Tools: introspection ──────────────────────

# ---- Telegram parse-mode fallback ----
# Telegram's legacy Markdown parser rejects an unescaped "_" inside a word
# (queued_for_digest, slugs, Baltic Times URLs) with 400 "can't parse
# entities". Rather than lose the message, retry once as HTML: escape the
# text and keep only *bold* / `code` formatting. Placeholders survive the
# conversion and resolve to <a href> links in HTML mode.

def _md_to_html_basic(text: str) -> str:
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    out = re.sub(r"\*([^*\n]+)\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    return out


def _tg_parse_failed(result: dict) -> bool:
    for r in result.get("results") or []:
        if isinstance(r, dict) and not r.get("ok"):
            desc = str(r.get("description") or r.get("raw") or "")
            if "parse" in desc.lower() or "entit" in desc.lower():
                return True
    return False


def _tg_all_ok(result: dict) -> bool:
    rs = result.get("results") or []
    return bool(rs) and all(isinstance(r, dict) and r.get("ok") for r in rs)


def _send_telegram_with_fallback(text: str, *, target: str,
                                 parse_mode: str = "Markdown",
                                 disable_preview: bool = False) -> dict:
    res = _send_telegram_text(text, target=target, parse_mode=parse_mode,
                              disable_preview=disable_preview)
    if parse_mode == "Markdown" and _tg_parse_failed(res):
        res = _send_telegram_text(_md_to_html_basic(text), target=target,
                                  parse_mode="HTML", disable_preview=disable_preview)
        res["fallback"] = "HTML"
    return res


def _send_comment_with_fallback(text: str, channel_msg_id: int,
                                parse_mode: str = "Markdown") -> dict:
    res = _send_telegram_comment(text, channel_msg_id, parse_mode=parse_mode)
    if parse_mode == "Markdown" and not res.get("error") and _tg_parse_failed(res):
        res = _send_telegram_comment(_md_to_html_basic(text), channel_msg_id,
                                     parse_mode="HTML")
        res["fallback"] = "HTML"
    return res


def _first_message_id(result: dict) -> int | None:
    try:
        return int(result["results"][0]["result"]["message_id"])
    except (KeyError, IndexError, TypeError, ValueError):
        return None


@mcp.tool()
def fetcher_status() -> dict:
    """
    Report on last fetcher run.

    Returns:
      inbox_count, inbox_generated_at, counts_by_source — inbox snapshot.
      last_fetch.errors — sources that returned an HTTP / network error.
      last_fetch.dead_sources — sources that are genuinely broken
        (fetch_error, no_entries, all_too_old). Excludes healthy-but-quiet
        sources whose entries were all duplicates.
      last_fetch.per_source[name] — full telemetry per source:
        health (ok | fetch_error | no_entries | no_dates | all_too_old |
        all_duplicate), entries_received, dated_with_fallback,
        dropped_too_old, dropped_dup_url, dropped_dup_title, added_new.
    """
    log_path = ROOT / "fetch_errors.json"
    inbox = _load_inbox()
    out: dict[str, Any] = {
        "inbox_count": len(inbox.get("articles", [])),
        "inbox_generated_at": inbox.get("generated_at"),
        "counts_by_source": inbox.get("counts_by_source", {}),
    }
    if log_path.exists():
        out["last_fetch"] = json.loads(log_path.read_text(encoding="utf-8"))
    return out



# ────────────────────── Tools: digest pipeline ──────────────────────
#
# Three tools that move the mechanical parts of prompts/digest.md out of the
# model's context:
#   digest_context     — ШАГ 1 + 1.5 in one call: compact cluster map, Baltic
#                        top-up by cluster, anti-repeat index + deterministic
#                        repeat markers, alerted/queued, run stats.
#   publish_digest     — ШАГ 5–7: the model hands over a JSON structure, the
#                        server renders Telegraph HTML, publishes, and posts
#                        the main-channel message from the TOC lines.
#   record_digest_run  — ШАГ 9: the server composes the run record from its
#                        own cache (stats, page_url, msg ids, anti-repeat
#                        counters) and applies the state patch.
# Everything the three tools need to share within one run lives in
# digest_run_cache.json (rewritten by digest_context at the start of a run).

BALTIC_OUTLETS = {"ERR", "Postimees", "The Baltic Times"}
ANTI_REPEAT_HOURS = 72
RELATED_STRONG = 2          # related_ids are similarity-sorted; only the
                            # strongest twins carry a "~" repeat hint
# ERR broadcast bulletins (radio/TV news programmes) cluster unrelated items
# under one programme title — noise for the Baltic top-up list.
_BROADCAST_RE = re.compile(
    r"(?i)(päevakaja|aktuaalne kaamera|vikerhommik|uudised kell|kell \d{1,2}[:.]\d{2})"
)
RUN_CACHE_PATH = ROOT / "digest_run_cache.json"

_INDEX_LEGEND = (
    "slug | section | app=<сколько выпусков подряд> last=<дата последнего "
    "разбора> <deep_dive|follow_up> [LIVE=активный кризис] | <заголовок> | "
    "<ключевые entities> [| new: <что было нового в последний раз>]"
)

_CLUSTER_LEGEND = (
    "id o=<независимых редакций> n=<статей> <языки> [S:<storyline_id>] "
    "[B=есть балтийская редакция ERR/Postimees/Baltic Times] "
    "[↻slug = детерминированный повтор темы из anti_repeat.index (общие "
    "article_ids/cluster_ids); ~slug = тот же повтор в кластере-близнеце на другом языке] | "
    "<заголовок> | s:<sample_ids через запятую — цитируй из них> "
    "r=<число related-близнецов> | <редакции>"
)


def _iso_z(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_run_cache() -> dict[str, Any]:
    if not RUN_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(RUN_CACHE_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return {}


def _save_run_cache(patch: dict[str, Any], *, reset: bool = False) -> dict[str, Any]:
    cache = {} if reset else _load_run_cache()
    cache.update(patch)
    tmp = RUN_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, RUN_CACHE_PATH)
    return cache


def _has_baltic_outlet(cluster: dict) -> bool:
    return bool(BALTIC_OUTLETS & set(cluster.get("outlets") or []))


def _build_anti_repeat_index(runs: list[dict]) -> dict[str, dict]:
    """
    Fold digest_runs[].topics into one record per slug (digest ШАГ 1.5).
    Descriptive fields come from the newest record; first_seen_run_ts is
    the earliest; appearances is the max stored counter (each stored value
    is already cumulative — summing them would double count).
    """
    index: dict[str, dict] = {}
    for run in sorted(runs, key=lambda r: str(r.get("ts", ""))):
        run_ts = str(run.get("ts", ""))
        for t in run.get("topics") or []:
            slug = t.get("slug")
            if not slug:
                continue
            e = index.get(slug)
            if e is None:
                e = index[slug] = {
                    "slug": slug,
                    "first_seen_run_ts": t.get("first_seen_run_ts") or run_ts,
                    "appearances": 0,
                    "runs_seen": 0,
                    "article_ids": set(),
                    "cluster_ids": set(),
                }
            e["runs_seen"] += 1
            e["appearances"] = max(e["appearances"], int(t.get("appearances") or 0))
            e["first_seen_run_ts"] = min(
                e["first_seen_run_ts"], t.get("first_seen_run_ts") or run_ts
            )
            e["last_run_ts"] = run_ts
            e["section"] = t.get("section")
            e["headline"] = t.get("headline")
            e["entities"] = t.get("entities") or []
            e["keywords"] = t.get("keywords") or []
            e["depth_last"] = t.get("depth")
            e["live_event"] = bool(t.get("live_event"))
            e["last_substantive_update"] = t.get("last_substantive_update")
            e["article_ids"] |= set(t.get("article_ids") or [])
            e["cluster_ids"] |= set(t.get("cluster_ids") or [])
    for e in index.values():
        e["appearances"] = max(e["appearances"], e["runs_seen"])
    return index


def _match_repeats(clusters: list[dict], index: dict[str, dict]
                   ) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Deterministic anti-repeat matching (digest ШАГ 3.0 / 4.0 bullet 2).

    direct:  cluster shares article ids with an indexed topic, or its id is
             in the topic's cluster_ids.
    related: no direct hit, but one of the two strongest related twins in
             a DIFFERENT language zone has one (en<->ru versions of one
             event). Same-language neighbours and storyline membership are
             not used — topical hubs would mark half the map.
    """
    aid2slugs: dict[str, set[str]] = {}
    cid2slugs: dict[str, set[str]] = {}
    for slug, e in index.items():
        for a in e["article_ids"]:
            aid2slugs.setdefault(a, set()).add(slug)
        for c in e["cluster_ids"]:
            cid2slugs.setdefault(c, set()).add(slug)
    direct: dict[str, set[str]] = {}
    for c in clusters:
        hits: set[str] = set(cid2slugs.get(c["id"], ()))
        for a in c.get("article_ids") or []:
            hits |= aid2slugs.get(a, set())
        if hits:
            direct[c["id"]] = hits
    by_id = {c["id"]: c for c in clusters}
    related: dict[str, set[str]] = {}
    for c in clusters:
        if c["id"] in direct:
            continue
        hits = set()
        my_langs = set(c.get("langs") or [])
        for r in (c.get("related_ids") or [])[:RELATED_STRONG]:
            if r not in direct:
                continue
            # the twin case the prompt cares about: the same event in
            # another language zone. Same-language "related" neighbours are
            # topical hubs (war sludge, EU-Ukraine) and mark far too much.
            if my_langs & set((by_id.get(r) or {}).get("langs") or []):
                continue
            hits |= direct[r]
        if hits:
            related[c["id"]] = hits
    return direct, related


def _cluster_line(c: dict, direct: dict, related: dict, *, full: bool) -> str:
    flags = []
    if c.get("storyline_id"):
        flags.append(f"S:{c['storyline_id']}")
    if _has_baltic_outlet(c):
        flags.append("B")
    if c["id"] in direct:
        flags.append("↻" + ",".join(sorted(direct[c["id"]])))
    elif c["id"] in related:
        flags.append("~" + ",".join(sorted(related[c["id"]])))
    outlets = [o.replace(" ", "") for o in c.get("outlets") or []]
    if len(outlets) > 5:
        outlets = outlets[:5] + [f"+{len(outlets) - 5}"]
    head = f"{c['id']} o={len(c.get('outlets') or [])} n={c.get('size')} " \
           f"{'/'.join(c.get('langs') or [])}"
    if flags:
        head += " " + " ".join(flags)
    title = (c.get("title") or "")[:100]
    if full:
        return (f"{head} | {title} | s:{','.join(c.get('sample_ids') or [])} "
                f"r={len(c.get('related_ids') or [])} | {','.join(outlets)}")
    return f"{head} | {title} | {','.join(outlets)}"


def _digest_window(hours: int) -> dict[str, Any]:
    """Clusters of the window + anti-repeat index and repeat markers.
    Shared by digest_context and digest_baltic_extra (cheap: ~0.2 s)."""
    data = _load_clusters()
    if not data:
        return {}
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    since_cmp = since.strftime("%Y-%m-%dT%H:%M:%S")  # prefix-comparable
    clusters = [c for c in data.get("clusters", [])
                if str(c.get("last_ts") or "")[:19] >= since_cmp]
    state = _load_state()
    cutoff_ar = _iso_z(now - timedelta(hours=ANTI_REPEAT_HOURS))
    runs = [r for r in state.get("digest_runs", []) if str(r.get("ts", "")) >= cutoff_ar]
    index = _build_anti_repeat_index(runs)
    direct, related = _match_repeats(clusters, index)
    return {"data": data, "now": now, "since": since, "since_cmp": since_cmp,
            "clusters": clusters, "state": state, "runs": runs,
            "index": index, "direct": direct, "related": related}


def _baltic_extra_clusters(clusters: list[dict], top: int) -> list[dict]:
    top_ids = {c["id"] for c in clusters[:top]}
    extra = [c for c in clusters if c["id"] not in top_ids and _has_baltic_outlet(c)
             and not _BROADCAST_RE.search(c.get("title") or "")]
    extra.sort(key=lambda c: (c.get("size", 0), c.get("last_ts") or ""), reverse=True)
    return extra


@mcp.tool()
def digest_context(hours: int = 24, top: int = 120) -> dict:
    """
    Everything the daily digest needs to start, in ONE call (replaces
    get_state + fetcher_status + list_clusters + the two Baltic list_news
    sweeps + hand-built anti-repeat index of digest ШАГ 1 / 1.5).

    Returns:
      now_iso / since_iso     — the window.
      stats                   — articles_in_window, sources_active,
                                clusters_in_window, multi_outlet_clusters,
                                inbox_count, dead_sources, fetch_errors.
      legend                  — format of the cluster lines below.
      top_clusters            — text, one line per cluster, the `top` most
                                covered events of the window (outlets, then
                                size). This is the map of the day.
      baltic_extra_total      — how many Baltic-newsroom clusters sit
                                OUTSIDE the top list; fetch them with
                                digest_baltic_extra() (separate call so
                                each response stays under the tool-result
                                size cap).
      anti_repeat             — window_hours, runs, topics; index = text,
                                one line per topic covered in the last 72h
                                (format in index_legend). Clusters that
                                deterministically repeat an indexed topic
                                carry ↻slug (shared article/cluster ids) or
                                ~slug (via related twins / storyline) in
                                their line — trust these over text matching.
      alerted_24h             — watchman alerts in the window.
      queued_for_digest       — items queued by watchman for this digest.
      last_digest_run         — ts of the previous digest.

    Side effect: starts a new run in digest_run_cache.json (stats and the
    anti-repeat index) for publish_digest / record_digest_run.
    If clusters.json is missing the tool returns {"error": ...} — fall back
    to the list_news path described in the prompt.
    """
    w = _digest_window(hours)
    if not w:
        return {"error": "clusters.json not found — clusterer has not run yet; "
                         "fall back to list_news"}
    data, now, since, since_cmp = w["data"], w["now"], w["since"], w["since_cmp"]
    clusters, state, runs = w["clusters"], w["state"], w["runs"]
    index, direct, related = w["index"], w["direct"], w["related"]
    since_iso = _iso_z(since)

    articles = _load_inbox().get("articles", [])
    in_window = [a for a in articles if str(a.get("published", ""))[:19] >= since_cmp]

    top_clusters = clusters[:top]
    extra_total = len(_baltic_extra_clusters(clusters, top))

    fetch_log: dict[str, Any] = {}
    log_path = ROOT / "fetch_errors.json"
    if log_path.exists():
        try:
            fetch_log = json.loads(log_path.read_text(encoding="utf-8"))
        except ValueError:
            fetch_log = {}
    stats = {
        "articles_in_window": len(in_window),
        "sources_active": len({a.get("source") for a in in_window}),
        "clusters_in_window": len(clusters),
        "multi_outlet_clusters": sum(1 for c in clusters if len(c.get("outlets") or []) >= 2),
        "inbox_count": len(articles),
        "clusters_generated_at": data.get("generated_at"),
        "dead_sources": fetch_log.get("dead_sources", []),
        "fetch_errors": fetch_log.get("errors", []),
    }

    alerted = [r for r in state.get("alerted", []) if str(r.get("ts", "")) >= since_iso]

    # Queue items carry the cluster id watchman saw hours or days ago. By
    # digest time that article may have left the 72h window or the cluster
    # may have been re-seeded; resolve each item to the CURRENT cluster so
    # readers are never spawned on a dead id (05.09.2026: reader on
    # art_9a44630a → «unknown cluster id», Finnish story dropped).
    art2cluster: dict[str, str] = {}
    for c in data.get("clusters", []):
        for aid in c.get("article_ids") or []:
            art2cluster.setdefault(aid, c["id"])
    queued = []
    for q in state.get("queued_for_digest", []):
        item = dict(q)
        qid = str(q.get("id") or "")
        live = art2cluster.get(qid)
        item["cluster_id"] = live
        item["status"] = "live" if live else "expired"
        queued.append(item)
    queued_expired = sum(1 for q in queued if q["status"] == "expired")

    # A missing run record (crash after publish, as on 03.09.2026) makes the
    # anti-repeat index blind to yesterday's issue. Flag it instead of
    # letting the model conclude «вчера выпуска не было».
    hours_since_last = None
    try:
        last = datetime.fromisoformat(str(state.get("last_digest_run")).replace("Z", "+00:00"))
        hours_since_last = round((now - last).total_seconds() / 3600, 1)
    except (TypeError, ValueError):
        pass
    gap_suspected = hours_since_last is not None and hours_since_last > 30

    # Compact on purpose: the whole response must stay well under the
    # client's tool-result cap (~25k tokens; Cyrillic is token-expensive).
    # One text line per indexed topic, see _INDEX_LEGEND.
    index_lines = []
    for slug, e in sorted(index.items(), key=lambda kv: kv[1].get("last_run_ts") or "",
                          reverse=True):
        upd = e.get("last_substantive_update")
        index_lines.append(
            f"{slug} | {e.get('section')} | app={e.get('appearances')} "
            f"last={str(e.get('last_run_ts') or '')[:10]} {e.get('depth_last') or '-'}"
            f"{' LIVE' if e.get('live_event') else ''} | "
            f"{(e.get('headline') or '')[:60]} | "
            f"{', '.join((e.get('entities') or [])[:3])}"
            f"{(' | new: ' + str(upd)[:70]) if upd else ''}"
        )
    # Freeze the map this run works with: the clusterer rewrites
    # clusters.json every fetch cycle and ids can change under the readers.
    try:
        CLUSTERS_SNAPSHOT_PATH.write_text(CLUSTERS_PATH.read_text(encoding="utf-8"),
                                          encoding="utf-8")
    except OSError:
        pass
    _save_run_cache({
        "started_at": _iso_z(now),
        "since_iso": since_iso,
        "hours": hours,
        "top": top,
        "stats": stats,
        "alerted_count": len(alerted),
        "queued_count": len(queued),
        "anti_repeat_index": {
            slug: {"first_seen_run_ts": e["first_seen_run_ts"],
                   "appearances": e["appearances"]}
            for slug, e in index.items()
        },
        "anti_repeat_matched_clusters": sorted(direct),
    }, reset=True)

    return {
        "now_iso": _iso_z(now),
        "since_iso": since_iso,
        "stats": stats,
        "legend": _CLUSTER_LEGEND,
        "top_clusters_count": len(top_clusters),
        "top_clusters": "\n".join(
            _cluster_line(c, direct, related, full=True) for c in top_clusters),
        "baltic_extra_total": extra_total,
        "anti_repeat": {
            "window_hours": ANTI_REPEAT_HOURS,
            "runs": len(runs),
            "topics": len(index),
            "clusters_direct": len(direct),
            "clusters_related": len(related),
            "index_legend": _INDEX_LEGEND,
            "index": "\n".join(index_lines),
        },
        "alerted_24h": alerted,
        "queued_for_digest": queued,
        "queued_legend": ("id = что записал watchman; cluster_id = АКТУАЛЬНЫЙ id кластера "
                          "для get_cluster / reader_packet (может отличаться от id); "
                          "status=expired — статья уже вне 72ч-окна, читателя не запускать, "
                          "тему можно упомянуть только по topic"),
        "queued_expired": queued_expired,
        "last_digest_run": state.get("last_digest_run"),
        "hours_since_last_run": hours_since_last,
        "gap_suspected": gap_suspected,
        "gap_note": ("ЗАПИСИ прогона за прошлые сутки нет (>30ч с last_digest_run). "
                     "Это почти наверняка значит, что выпуск ВЫШЕЛ, но прогон оборвался "
                     "до записи: anti-repeat его не видит. Темы, которые в норме попали бы "
                     "во вчерашний выпуск (крупные, o>=3, начавшиеся >24ч назад), веди как "
                     "повтор (режим B/C), а в комментарии-методологии не утверждай, что "
                     "выпуска не было — пиши «последняя запись прогона — <дата>»."
                     if gap_suspected else None),
    }


@mcp.tool()
def digest_baltic_extra(limit: int = 250) -> dict:
    """
    БАЛТИЙСКИЙ ДОБОР — the second half of digest_context: clusters of the
    same window that are OUTSIDE the top list but have a Baltic newsroom
    (ERR / Postimees / Baltic Times), largest first. Local Estonian topics
    covered by a single newsroom live here; work with them through their
    cluster id (get_cluster). Same line format and ↻/~ repeat markers as
    top_clusters (see digest_context.legend), without sample ids.
    Uses the window/top of the last digest_context call (24h / 180 by
    default). ERR broadcast bulletins (Päevakaja, Aktuaalne kaamera) are
    filtered out.
    """
    cache = _load_run_cache()
    w = _digest_window(int(cache.get("hours") or 24))
    if not w:
        return {"error": "clusters.json not found — clusterer has not run yet"}
    extra = _baltic_extra_clusters(w["clusters"], int(cache.get("top") or 180))
    return {
        "count": min(limit, len(extra)),
        "total": len(extra),
        "clusters": "\n".join(
            _cluster_line(c, w["direct"], w["related"], full=False)
            for c in extra[:limit]),
    }


# ---- publish_digest: structure → Telegraph HTML + main post ----

def _sources_line(ids: list[str] | None) -> str:
    ids = [i for i in (ids or []) if i]
    return "Источники: " + " · ".join(f"[[{i}]]" for i in ids) if ids else ""


def _render_deep_dive(item: dict) -> str:
    parts = [f"<h4>{item.get('headline', '')}</h4>"]
    if item.get("essence"):
        parts.append(f"<p><b>Суть.</b> {item['essence']}</p>")
    sides = [s for s in (item.get("sides") or []) if s.get("text")]
    if sides:
        parts.append("<p><b>Что подчёркивают разные стороны.</b></p>")
        parts.append("<ul>" + "".join(
            f"<li>{(s.get('label') + ': ') if s.get('label') else ''}{s['text']}</li>"
            for s in sides) + "</ul>")
    if item.get("averaged"):
        parts.append(f"<p><b>Усреднённое.</b> {item['averaged']}</p>")
    if item.get("note"):
        parts.append(f"<p><i>{item['note']}</i></p>")
    if src := _sources_line(item.get("sources")):
        parts.append(f"<p>{src}</p>")
    return "\n".join(parts)


def _render_brief(item: dict) -> str:
    parts = [f"<h4>{item.get('headline', '')}</h4>"]
    text = (item.get("text") or "").strip()
    src = _sources_line(item.get("sources"))
    parts.append(f"<p>{text}{' ' if text and src else ''}{src}</p>")
    if item.get("delta"):
        parts.append(f"<p>{item['delta']}</p>")
    return "\n".join(parts)


def _render_followups(items: list[dict] | None, heading: str) -> str:
    items = [i for i in (items or []) if i.get("headline")]
    if not items:
        return ""
    out = [f"<h4>{heading}</h4>"]
    for i in items:
        src = _sources_line(i.get("sources"))
        out.append(f"<p><b>↻ {i['headline']}</b> — что нового: "
                   f"{i.get('whats_new', '')}{(' ' + src) if src else ''}</p>")
    return "\n".join(out)


def _toc_text(item: dict) -> str:
    return (item.get("toc") or item.get("headline") or "").strip()


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _render_digest_html(stats: dict, alert_topics: list[str] | None,
                        baltic: list[dict], baltic_fu: list[dict] | None,
                        world: list[dict], world_fu: list[dict] | None,
                        tech: list[dict], tech_fu: list[dict] | None) -> str:
    h: list[str] = []
    h.append(f"<p><i>Обработано {stats.get('articles_in_window', '?')} статей в "
             f"{stats.get('clusters_in_window', '?')} событиях · "
             f"источников активно: {stats.get('sources_active', '?')}</i></p>")
    if alert_topics:
        n = len(alert_topics)
        word = ("был 1 срочный алерт" if n == 1 else
                f"было {n} срочных алерта" if 2 <= n <= 4 else
                f"было {n} срочных алертов")
        h.append(f"<p>🚨 <b>За сутки {word}:</b> "
                 f"{', '.join(alert_topics)}. Подробности — в чате выше.</p>")
    h.append("<h4>📌 В этом выпуске</h4>")
    for anchor, label, items in (("Baltic", "🇪🇪 Эстония и Финляндия", baltic),
                                 ("World", "🌍 Мир", world),
                                 ("Tech", "💻 Технологии и AI", tech)):
        h.append(f'<p><b><a href="#{anchor}">{label}</a></b></p>')
        h.append("<ul>" + "".join(f"<li>{_html_escape(_toc_text(i))}</li>" for i in items) + "</ul>")
    h.append("<hr/>")
    h.append('<h3 id="Baltic">🇪🇪 Эстония и Финляндия — разбор дня</h3>')
    h.append("\n<hr/>\n".join(_render_deep_dive(i) for i in baltic))
    if fu := _render_followups(baltic_fu, "↻ Продолжение сюжетов"):
        h.append("<hr/>\n" + fu)      # separate from the last deep-dive
    h.append('<h3 id="World">🌍 Мир</h3>')
    h.extend(_render_brief(i) for i in world)
    if fu := _render_followups(world_fu, "↻ Продолжение сюжетов мира"):
        h.append("<hr/>\n" + fu)
    h.append('<h3 id="Tech">💻 Технологии и AI</h3>')
    h.extend(_render_brief(i) for i in tech)
    if fu := _render_followups(tech_fu, "↻ Продолжение тех-сюжетов"):
        h.append("<hr/>\n" + fu)
    return "\n".join(h)


def _render_main_post(date_label: str, baltic: list[dict], world: list[dict],
                      tech: list[dict], page_url: str) -> str:
    def block(title: str, items: list[dict]) -> str:
        return f"<b>{title}</b>\n" + "\n".join(
            "• " + _html_escape(_toc_text(i)) for i in items)
    return (f"📰 <b>Дайджест за {date_label}</b>\n\n"
            f"{block('🇪🇪 Эстония и Финляндия', baltic)}\n\n"
            f"{block('🌍 Мир', world)}\n\n"
            f"{block('💻 Технологии', tech)}\n\n"
            f'👉 <a href="{page_url}">Полный разбор</a>')


@mcp.tool()
def publish_digest(
    baltic: list[dict],
    world: list[dict],
    tech: list[dict],
    baltic_followups: list[dict] | None = None,
    world_followups: list[dict] | None = None,
    tech_followups: list[dict] | None = None,
    alert_topics: list[str] | None = None,
    title: str | None = None,
    dry_run: bool = False,
    methodology: str | None = None,
    topics: list[dict] | None = None,
    notes: str = "",
    log_extra: str = "",
) -> dict:
    """
    Render the digest from a structure, publish it to Telegraph and post the
    main-channel message (digest ШАГ 5–7 in one call). The server owns the
    HTML template, the TOC, anchors, the header stats line and the main post
    template — the model only supplies the content.

    ONE-CALL FINISH (preferred): pass `methodology` and `topics` too and the
    server also runs ШАГ 7.5 + 8 + 9 right after the post, in this order:
      methodology — text of the «🤖 Как собирался этот выпуск» comment
                    (Markdown; on a Telegram parse error the server retries
                    it as HTML). Posted under the main post.
      topics[]    — the record_digest_run topics (one per published item,
                    see that tool); notes — 1–2 sentence run summary.
      log_extra   — optional extra lines for the tech log (anti-repeat
                    A/B/C counts, reader notes); the server composes the
                    rest of the 📋 Digest run log from its run cache.
    The run is recorded even if the comment fails (discussion_msg_id stays
    null, the error is returned in `comment`). Without `topics` the call
    behaves as before and ШАГ 7.5 / 8 / 9 must be done by separate calls.

    Item shapes (text fields may contain [[art_id]] placeholders and the
    Telegraph-safe inline tags b/i/em/strong; never hand-written URLs):
      baltic[]  deep-dive: {headline, toc, essence, sides: [{label, text}],
                averaged, note, sources: [art_ids]}
                — sides/averaged/note optional (single-source story:
                essence + note «пока освещает только X»).
      world[] / tech[]: {headline, toc, text, delta, sources: [art_ids]}
                — delta optional: ONE line ⚔/👁/🤐/📢 or omit.
      *_followups[]: {headline, whats_new, sources: [art_ids]} — mode B
                items; omit or [] when there were none (no heading is
                rendered then).
      toc — short line (≤90 chars, no links) used both in the Telegraph
            «📌 В этом выпуске» list and as the bullet of the main post;
            defaults to headline.
      alert_topics — short names of watchman alerts of the last 24h for the
            🚨 header line; omit/[] when there were none.
      title — defaults to «Дайджест DD.MM.YYYY» with the local run date.

    Returns {url, channel_msg_id, main_post_result, missing_placeholders,
    html_chars}. Sends nothing when dry_run=True (returns html + main post
    text instead). On Telegraph failure returns {"error": ...} and sends
    nothing — use the fallback described in the prompt.
    """
    cache = _load_run_cache()
    stats = cache.get("stats") or {}
    date_label = datetime.now().strftime("%d.%m.%Y")
    title = title or f"Дайджест {date_label}"
    html = _render_digest_html(stats, alert_topics, baltic, baltic_followups,
                               world, world_followups, tech, tech_followups)
    if dry_run:
        resolved, missing = _resolve_placeholders(html, html=True)
        return {
            "title": title,
            "html": html,
            "html_resolved": resolved,
            "missing_placeholders": missing,
            "main_post": _render_main_post(date_label, baltic, world, tech,
                                           "https://telegra.ph/DRY-RUN"),
        }
    # Unresolvable placeholders (a cluster/twin id used as an article id, or
    # an article that left the window) must not reach the page as
    # «[?art_…]» (05.09.2026: twice on the published page). Strip them,
    # keep the text, report in the tech log.
    _resolved_probe, unresolved = _resolve_placeholders(html, html=True)
    if unresolved:
        for bad in set(unresolved):
            html = re.sub(r"\s*·\s*\[\[" + re.escape(bad) + r"\]\]", "", html)
            html = re.sub(r"\s*\[\[" + re.escape(bad) + r"\]\]", "", html)
        html = re.sub(r"Источники:\s*</p>", "</p>", html)
        _save_run_cache({"unresolved_placeholders": sorted(set(unresolved))})
    published = _publish_telegraph_html(title, html)
    if published.get("error"):
        return published
    page_url = published["url"]
    main_text = _render_main_post(date_label, baltic, world, tech, page_url)
    sent = _send_telegram_text(main_text, target="main", parse_mode="HTML",
                               disable_preview=False)
    channel_msg_id = None
    try:
        channel_msg_id = sent["results"][0]["result"]["message_id"]
    except (KeyError, IndexError, TypeError):
        pass
    _save_run_cache({
        "page_url": page_url,
        "main_msg_id": channel_msg_id,
        "published_at": _iso_z(datetime.now(timezone.utc)),
        "published_counts": {
            "baltic": len(baltic), "world": len(world), "tech": len(tech),
            "baltic_followups": len(baltic_followups or []),
            "world_followups": len(world_followups or []),
            "tech_followups": len(tech_followups or []),
        },
    })
    out = {
        "url": page_url,
        "channel_msg_id": channel_msg_id,
        "main_post_ok": _tg_all_ok(sent),
        "missing_placeholders": published.get("missing_placeholders", []),
        "html_chars": len(html),
    }
    if not out["main_post_ok"]:
        out["main_post_result"] = sent
    if methodology is None and topics is None:
        return out
    out.update(_finish_digest(page_url, channel_msg_id, methodology, topics,
                              notes, log_extra))
    return out


def _finish_digest(page_url: str, channel_msg_id: int | None,
                   methodology: str | None, topics: list[dict] | None,
                   notes: str, log_extra: str) -> dict:
    """ШАГ 7.5 (comment) + ШАГ 8 (tech log) + ШАГ 9 (record) after a
    successful publish_digest. Each part reports its own error; the run
    record is written whenever `topics` is given."""
    out: dict[str, Any] = {}
    discussion_msg_id: int | None = None
    if methodology:
        if channel_msg_id is None:
            out["comment"] = {"error": "main post has no message_id — comment skipped"}
        else:
            c = _send_comment_with_fallback(methodology, channel_msg_id)
            if c.get("error"):
                out["comment"] = {"error": c["error"]}
            else:
                discussion_msg_id = c.get("discussion_msg_id")
                out["comment"] = {"discussion_msg_id": discussion_msg_id,
                                  "ok": _tg_all_ok(c), "fallback": c.get("fallback")}
                if not _tg_all_ok(c):
                    out["comment"]["results"] = c.get("results")

    cache = _load_run_cache()
    stats = cache.get("stats") or {}
    counts = cache.get("published_counts") or {}
    dead = stats.get("dead_sources") or []
    log_lines = [
        f"📋 Digest run {datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"✅ Опубликовано: {page_url}",
        f"📊 Статей в inbox: {stats.get('inbox_count')} · в окне 24ч: "
        f"{stats.get('articles_in_window')} · кластеров: {stats.get('clusters_in_window')} "
        f"· источников активно: {stats.get('sources_active')} · ошибок фетча: "
        f"{len(stats.get('fetch_errors') or [])}",
        f"❌ Мёртвые/недоступные: {', '.join(dead) if dead else 'нет'}",
        f"📈 Baltic {counts.get('baltic', 0)} deep-dive + {counts.get('baltic_followups', 0)} follow-up "
        f"· World {counts.get('world', 0)} + {counts.get('world_followups', 0)} "
        f"· Tech {counts.get('tech', 0)} + {counts.get('tech_followups', 0)} "
        f"· anti-repeat matched: {len(cache.get('anti_repeat_matched_clusters') or [])} "
        f"· алертов за сутки: {cache.get('alerted_count', 0)} · очередь: {cache.get('queued_count', 0)}",
    ]
    if out.get("comment", {}).get("error"):
        log_lines.append(f"⚠ Комментарий-методология не отправлен: {out['comment']['error']}")
    if bad := cache.get("unresolved_placeholders"):
        log_lines.append("⚠ Вырезаны нерезолвящиеся плейсхолдеры (не article_id или статья вне "
                         f"окна): {', '.join(bad)}")
    if log_extra:
        log_lines.append(log_extra.strip())
    log = _send_telegram_text(_md_to_html_basic("\n".join(log_lines)), target="log",
                              parse_mode="HTML", disable_preview=True)
    out["tech_log_ok"] = _tg_all_ok(log)
    if not out["tech_log_ok"]:
        out["tech_log_result"] = log

    if topics is not None:
        out["record"] = _record_digest_run(topics, notes=notes,
                                           discussion_msg_id=discussion_msg_id)
    return out


# ---- verify_card: reader fact-cards are checked against the texts ----

_NORM_MAP = str.maketrans({
    "«": '"', "»": '"', "“": '"', "”": '"', "„": '"', "‟": '"',
    "‘": "'", "’": "'", "‚": "'", "—": "-", "–": "-", "−": "-",
    "\u00a0": " ", "\u202f": " ", "ё": "е", "Ё": "е",
})


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").translate(_NORM_MAP)).strip().lower()


def _quote_found(quote: str, hay: str) -> bool:
    """Every ellipsis-separated fragment of the quote must appear verbatim
    (after normalisation) in the haystack."""
    parts = [p.strip(" .,;:!?\"'-") for p in re.split(r"\.{3}|…", _norm(quote))]
    parts = [p for p in parts if len(p) >= 4]
    return bool(parts) and all(p in hay for p in parts)


_NUM_RE = re.compile(r"\d[\d\s.,]*")


def _number_found(value: str, hay: str) -> bool:
    """The numeric core of `value` ("31 млн" -> 31, "4%" -> 4, "12,5" ->
    12,5) must appear in the haystack as a standalone number."""
    m = _NUM_RE.search(_norm(value))
    if not m:
        return _norm(value) in hay
    core = m.group(0).strip(" .,")
    variants = {core, core.replace(" ", ""), core.replace(",", "."),
                core.replace(".", ","), core.replace(" ", "\u00a0")}
    # Thousands separators differ by language: BBC/Guardian "50,000", ERR
    # "50 000", TASS "50 тыс." — the digest of 04.09.2026 wrote «50 000»
    # against an English "50,000" source and the check failed. Treat a
    # grouped number and its bare digits as the same number.
    grouped = re.fullmatch(r"\d{1,3}(?:[ ,.\u00a0]\d{3})+", core)
    if grouped or re.fullmatch(r"\d{4,}", core):
        digits = re.sub(r"[ ,.\u00a0]", "", core)
        variants.add(digits)
        for sep in (",", " ", ".", "\u00a0"):
            variants.add(f"{int(digits):,}".replace(",", sep))
    return any(re.search(r"(?<![\d,.])" + re.escape(v) + r"(?![\d])", hay)
               for v in variants if v)


def _haystack(article_id: str, cache: dict[str, str | None]) -> str | None:
    if article_id in cache:
        return cache[article_id]
    inbox = _index_inbox()
    art = inbox.get(article_id)
    if not art:
        cache[article_id] = None
        return None
    text, _cached, _err = _article_text(article_id, want_chars=READ_FULL_CACHE_CHARS)
    hay = _norm(" ".join(filter(None, [art.get("title"), art.get("summary"), text or ""])))
    cache[article_id] = hay
    return hay


@mcp.tool()
def verify_card(card: dict) -> dict:
    """
    Check a reader's fact card against the article texts and drop what
    cannot be found — the guard against invented quotes, computed numbers
    and fabricated "disagreements" (both seen in the 02.09.2026 experiment).

    card fields checked (others pass through untouched):
      quotes[]        {text, who, article_id} — kept only if every
                      ellipsis-separated fragment of `text` appears
                      verbatim (case/quote-mark/whitespace-insensitive) in
                      that article's title+summary+full text.
      numbers[]       {value, meaning, article_id} — kept only if the
                      numeric core of `value` appears in that article.
      disagreements[] {claim, evidence: [{article_id, quote}]} — kept only
                      if >= 2 evidence quotes verify, from >= 2 distinct
                      articles. A plain-string disagreement is dropped.
    Articles not in the inbox / not fetchable count as "not found".

    Returns {card: <cleaned card>, kept: {...}, dropped: {quotes, numbers,
    disagreements}, articles_missing: [...]} — the reader returns `card`
    as its final answer; the drop lists go into its notes.
    """
    hay_cache: dict[str, str | None] = {}
    missing: set[str] = set()
    out = dict(card)
    dropped: dict[str, list] = {"quotes": [], "numbers": [], "disagreements": []}

    def hay_for(aid: str) -> str | None:
        h = _haystack(aid, hay_cache)
        if h is None:
            missing.add(aid)
        return h

    kept_quotes = []
    for q in card.get("quotes") or []:
        h = hay_for(str(q.get("article_id") or ""))
        if h and _quote_found(str(q.get("text") or ""), h):
            kept_quotes.append(q)
        else:
            dropped["quotes"].append({**q, "reason": "not in article text"})
    out["quotes"] = kept_quotes

    kept_numbers = []
    for n in card.get("numbers") or []:
        h = hay_for(str(n.get("article_id") or ""))
        if h and _number_found(str(n.get("value") or ""), h):
            kept_numbers.append(n)
        else:
            dropped["numbers"].append({**n, "reason": "number not in article text"})
    out["numbers"] = kept_numbers

    kept_dis = []
    for d in card.get("disagreements") or []:
        if not isinstance(d, dict):
            dropped["disagreements"].append({"claim": str(d), "reason": "no evidence quotes"})
            continue
        ok_ids = set()
        verified_evidence = []
        for ev in d.get("evidence") or []:
            aid = str(ev.get("article_id") or "")
            h = hay_for(aid)
            if h and _quote_found(str(ev.get("quote") or ""), h):
                ok_ids.add(aid)
                verified_evidence.append(ev)
        if len(ok_ids) >= 2:
            kept_dis.append({**d, "evidence": verified_evidence})
        else:
            dropped["disagreements"].append({
                **d, "reason": f"only {len(ok_ids)} verified evidence article(s), need 2"})
    out["disagreements"] = kept_dis

    return {
        "card": out,
        "kept": {"quotes": len(kept_quotes), "numbers": len(kept_numbers),
                 "disagreements": len(kept_dis)},
        "dropped": dropped,
        "articles_missing": sorted(missing),
    }


# ---- reader_packet: one call replaces get_cluster + N × read_full ----

READER_MAX_ARTICLES = 4
READER_MAX_CHARS = 8000
READER_TOTAL_CHARS = 36000     # keeps the tool result under the client cap
READER_META_CAP = 40           # metadata rows returned (largest clusters)


def _source_group(source: str) -> str:
    """Editorial camp of a feed, for picking texts from DIFFERENT camps."""
    src = (source or "").lower()
    if src.startswith(("err", "postimees")) or "baltic times" in src:
        return "baltic"
    if src in ("tass", "ria"):
        return "russian"
    if src.startswith(("unian", "pravda")):
        return "ukrainian"
    return "western"


def _pick_reader_texts(articles: list[dict], max_articles: int) -> list[dict]:
    """Up to max_articles articles, distinct outlets, camps in the order
    the reader prompt asks for (Baltic, Western, Russian, Ukrainian), then
    round-robin over the remaining camps. Within a camp the newest article
    goes first (usually the most complete version)."""
    by_group: dict[str, list[dict]] = {}
    for a in sorted(articles, key=lambda a: str(a.get("published") or ""), reverse=True):
        by_group.setdefault(_source_group(a.get("source")), []).append(a)
    order = ["baltic", "western", "russian", "ukrainian"]
    picked: list[dict] = []
    used_outlets: set[str] = set()
    while len(picked) < max_articles:
        progressed = False
        for g in order:
            if len(picked) >= max_articles:
                break
            for a in by_group.get(g, []):
                outlet = _source_group(a["source"]) + ":" + re.sub(
                    r"\s+(ru|est|rus|news|en|ee|ua|world|intl|markets|china)$", "",
                    a["source"].strip().lower())
                if a in picked or outlet in used_outlets:
                    continue
                picked.append(a)
                used_outlets.add(outlet)
                progressed = True
                break
        if not progressed:
            # all outlets used once — allow a second article per outlet
            rest = [a for g in order for a in by_group.get(g, []) if a not in picked]
            if not rest:
                break
            picked.append(rest[0])
    return picked[:max_articles]


@mcp.tool()
def reader_packet(
    cluster_id: str,
    twin_ids: list[str] | None = None,
    max_articles: int = READER_MAX_ARTICLES,
    max_chars: int = READER_MAX_CHARS,
) -> dict:
    """
    Everything a reader (prompts/reader.md) needs in ONE call: the cluster,
    its explicitly passed twins, the metadata of every article of the
    event, and the FULL TEXTS of up to `max_articles` articles chosen from
    different editorial camps (Baltic → Western → Russian → Ukrainian,
    distinct outlets, newest first). Replaces get_cluster + N × read_full.

    Args:
        cluster_id: the event cluster.
        twin_ids: cluster ids of the same event in other languages (from
            the digest map / related_ids). Merged into the same packet.
        max_articles / max_chars: how many texts and how long each (cut at
            max_chars; `truncated` + `total_chars` say whether more exists
            — read_full(id, max_chars=20000) for a quote from the tail).

    Returns:
        cluster            — id/title/size/outlets/langs/first_ts/last_ts
        twins_included     — twin cluster ids merged
        related_candidates — other related clusters (id/title/langs/
                             outlets/size) NOT merged: pull one in with
                             get_cluster/read_full only if its title is
                             clearly the same event.
        articles           — metadata of the event's articles
                             (id/source/title/summary/published)
        texts              — [{id, source, title, text, truncated,
                             total_chars}] — quote from THESE; every quote /
                             number must be a verbatim fragment of one of
                             them (verify_card checks exactly that).
        articles_failed    — [{id, source, error}] texts that could not be
                             fetched (paywall/403) — use their summary only.
    """
    data = _load_clusters()
    if not data:
        return {"error": "clusters.json not found — clusterer has not run yet"}
    by_id = {c.get("id"): c for c in data.get("clusters", [])}
    cluster, how = _find_cluster(cluster_id, data)
    if cluster is None:
        return {"error": f"unknown cluster id: {cluster_id} — not in the live "
                         "map, not a member of any live cluster, not in the "
                         "digest snapshot"}
    inbox = _index_inbox()

    twins: list[dict] = []
    twins_missing: list[str] = []
    READER_MAX_TWINS = 5
    twins_ignored = list(twin_ids or [])[READER_MAX_TWINS:]
    for t in (twin_ids or [])[:READER_MAX_TWINS]:
        c, _how = _find_cluster(t, data)
        if c is None:
            twins_missing.append(t)
        elif c is not cluster and c.get("id") != cluster.get("id") and c not in twins:
            twins.append(c)
    merged_ids: list[str] = []
    for c in [cluster, *twins]:
        for aid in c.get("article_ids") or []:
            if aid not in merged_ids:
                merged_ids.append(aid)
    articles = [inbox[a] for a in merged_ids if a in inbox]
    expired = [a for a in merged_ids if a not in inbox]

    related = []
    for rid in (cluster.get("related_ids") or [])[:6]:
        c = by_id.get(rid)
        if c is None or c in twins:
            continue
        related.append({"id": rid, "title": (c.get("title") or "")[:120],
                        "langs": c.get("langs"), "outlets": c.get("outlets"),
                        "size": c.get("size")})

    max_articles = max(1, min(int(max_articles), 8))
    max_chars = max(1000, min(int(max_chars), READ_FULL_CACHE_CHARS))
    texts, failed = [], []
    budget = READER_TOTAL_CHARS
    candidates = _pick_reader_texts(articles, max_articles)
    # keep a reserve list so a failed fetch is replaced by the next camp
    reserve = [a for a in _pick_reader_texts(articles, min(8, len(articles)))
               if a not in candidates]
    queue = candidates + reserve
    for a in queue:
        if len(texts) >= max_articles or budget <= 1000:
            break
        text, _cached, err = _article_text(a["id"], want_chars=READ_FULL_CACHE_CHARS)
        if err or not text:
            failed.append({"id": a["id"], "source": a["source"], "error": err or "empty"})
            continue
        cut = min(max_chars, budget)
        texts.append({
            "id": a["id"], "source": a["source"], "title": a["title"],
            "published": a.get("published"),
            "text": text[:cut], "truncated": len(text) > cut, "total_chars": len(text),
        })
        budget -= len(text[:cut])

    meta = sorted(articles, key=lambda a: str(a.get("published") or ""), reverse=True)
    return {
        "cluster": {k: cluster.get(k) for k in
                    ("id", "title", "size", "outlets", "langs", "first_ts",
                     "last_ts", "storyline_id")},
        "twins_included": [t.get("id") for t in twins],
        "twins_missing": twins_missing,
        "twins_ignored": twins_ignored,
        "resolved_via": how,
        "related_candidates": related,
        "articles_total": len(articles),
        "articles": [{k: a.get(k) for k in ("id", "source", "title", "summary", "published")}
                     for a in meta[:READER_META_CAP]],
        "expired_ids": expired,
        "texts": texts,
        "articles_failed": failed,
    }


# ---- record_digest_run: ШАГ 9 ----

_TOPIC_REQUIRED = ("slug", "section", "headline", "article_ids", "depth")


@mcp.tool()
def record_digest_run(
    topics: list[dict],
    notes: str = "",
    discussion_msg_id: int | None = None,
    extra: dict | None = None,
) -> dict:
    """
    Close the digest run (ШАГ 9): compose the digest_runs record and apply
    the state patch (clear queued_for_digest, last_digest_run, append the
    run, prune alerted older than 24h) in one atomic write.

    topics[] — one per PUBLISHED item (deep-dive or follow-up; skipped
    mode-C topics are NOT recorded):
      {slug, section: baltic|world|tech, headline, entities[], keywords[],
       article_ids[], cluster_ids[], depth: deep_dive|follow_up,
       last_substantive_update|null, live_event: bool}
    first_seen_run_ts and appearances are filled by the server from the
    anti-repeat index of this run (slug match) — do not pass them.
    notes — 1–2 sentence summary of the run.
    discussion_msg_id — from send_telegram_comment (ШАГ 7.5); the server
    also remembers it itself, so pass it only if you have it.
    extra — optional additional fields merged into the run record.

    page_url, main_msg_id, stats, alert/queue counters and section counts
    come from the server's run cache (digest_context + publish_digest).
    """
    return _record_digest_run(topics, notes=notes,
                              discussion_msg_id=discussion_msg_id, extra=extra)


def _record_digest_run(
    topics: list[dict],
    notes: str = "",
    discussion_msg_id: int | None = None,
    extra: dict | None = None,
) -> dict:
    """Body of record_digest_run — shared with publish_digest."""
    cache = _load_run_cache()
    now = datetime.now(timezone.utc)
    ts = _iso_z(now)
    index = cache.get("anti_repeat_index") or {}

    bad = [t.get("slug") or "?" for t in topics
           if any(not t.get(k) for k in _TOPIC_REQUIRED)]
    if bad:
        return {"error": f"topics missing one of {_TOPIC_REQUIRED}: {bad} — "
                         "state.json was NOT modified"}

    new_topics = []
    for t in topics:
        prev = index.get(t["slug"])
        rec = {
            "slug": t["slug"],
            "section": t["section"],
            "headline": t["headline"],
            "entities": t.get("entities") or [],
            "keywords": t.get("keywords") or [],
            "article_ids": t.get("article_ids") or [],
            "cluster_ids": t.get("cluster_ids") or [],
            "depth": t["depth"],
            "first_seen_run_ts": (prev or {}).get("first_seen_run_ts") or ts,
            "appearances": int((prev or {}).get("appearances") or 0) + 1,
            "last_substantive_update": t.get("last_substantive_update"),
            "live_event": bool(t.get("live_event")),
        }
        new_topics.append(rec)

    def count(section: str, depth: str) -> int:
        return sum(1 for t in new_topics if t["section"] == section and t["depth"] == depth)

    stats = cache.get("stats") or {}
    run = {
        "ts": ts,
        "run": "digest",
        "format": "telegraph",
        "page_url": cache.get("page_url"),
        "articles_processed": stats.get("articles_in_window"),
        "sources_in_window": stats.get("sources_active"),
        "clusters_in_window": stats.get("clusters_in_window"),
        "baltic_deep_dives": count("baltic", "deep_dive"),
        "baltic_followups": count("baltic", "follow_up"),
        "world_items": count("world", "deep_dive"),
        "world_followups": count("world", "follow_up"),
        "tech_items": count("tech", "deep_dive"),
        "tech_followups": count("tech", "follow_up"),
        "alerted_summary_emitted": bool(cache.get("alerted_count")),
        "alerted_summary_count": cache.get("alerted_count", 0),
        "queued_items_consumed_approx": cache.get("queued_count", 0),
        "main_msg_id": cache.get("main_msg_id"),
        "discussion_msg_id": (discussion_msg_id
                              if discussion_msg_id is not None
                              else cache.get("discussion_msg_id")),
        "test_mode": bool(_load_config().get("test_mode")),
        "anti_repeat_matched": len(cache.get("anti_repeat_matched_clusters") or []),
        "topics": new_topics,
        "notes": notes,
    }
    if extra:
        run.update(extra)

    result = _apply_state_patch({
        "clear_queued": True,
        "last_digest_run": ts,
        "append_digest_run": run,
    })
    if result.get("error"):
        return result
    state = _load_state()
    cutoff = now.timestamp() - 24 * 3600
    kept = []
    for rec in state.get("alerted", []):
        try:
            if datetime.fromisoformat(str(rec.get("ts", ""))).timestamp() >= cutoff:
                kept.append(rec)
        except ValueError:
            pass
    removed = len(state.get("alerted", [])) - len(kept)
    state["alerted"] = kept
    _write_state(state)
    _save_run_cache({"recorded_at": ts})
    return {
        "recorded": True,
        "ts": ts,
        "topics_recorded": len(new_topics),
        "digest_runs_count": result.get("digest_runs_count"),
        "alerted_pruned": removed,
        "page_url": run["page_url"],
        "main_msg_id": run["main_msg_id"],
        "discussion_msg_id": run["discussion_msg_id"],
        "counts": {k: run[k] for k in (
            "baltic_deep_dives", "baltic_followups", "world_items",
            "world_followups", "tech_items", "tech_followups")},
    }


# ────────────────────── Tools: watchman pipeline ──────────────────────
# Same idea as digest_context / publish_digest: the hourly watchman used
# to spend 12–16 agent turns on get_state + cleanup_state + list_clusters
# (51 KB of JSON) + send_telegram + update_state (18 KB of state echoed
# back). Two calls now: watchman_context (everything to decide, compact
# text lines) and watchman_finish (alerts + comments + log + state patch).

WATCHMAN_QUEUE_LINES = 60
_WATCHMAN_LEGEND = (
    "id o=<независимых редакций> n=<статей> <языки> [S:<storyline_id>] "
    "[B=есть балтийская редакция] | <заголовок> | s:<sample_ids — статьи "
    "РАЗНЫХ редакций, цитируй из них> r=<число кластеров-близнецов> | "
    "<редакции через запятую>"
)


@mcp.tool()
def watchman_context(limit: int = 150, fallback_minutes: int = 60,
                     max_age_hours: int = 24) -> dict:
    """
    Everything the hourly watchman needs to decide, in ONE call (replaces
    get_state + cleanup_state(24) + list_clusters).

    Side effect: prunes state.alerted older than `max_age_hours` (what
    cleanup_state did). Does NOT move last_watchman_run — watchman_finish
    does that, so a crashed run is re-scanned next hour.

    Returns:
      now_iso / since_iso  — window: since = state.last_watchman_run, or
                             now − fallback_minutes when the state is empty.
      alerts_today         — {date, count} for today (5/day hard cap).
      alerted_24h          — alerts already pushed in the last 24h
                             (topic_key, headline, ts) — filter 3 (novelty).
      queued_total         — size of queued_for_digest.
      queued_recent        — text, one «id | topic» line per queued item,
                             newest last (the last WATCHMAN_QUEUE_LINES) —
                             don't re-queue the same story every hour.
      legend / clusters    — text, one line per cluster with activity in
                             the window, most covered first (outlets, then
                             size). o=1 lines are single-newsroom routine;
                             scan their titles quickly. related twins are
                             only counted (r=N): get_cluster(id) when a
                             single-outlet story may be confirmed in its
                             other-language twin (filter 2).
      clusters_count / window_total / truncated.
    {"error": ...} when clusters.json is missing — fall back to list_news.
    """
    state = _load_state()
    now = datetime.now(timezone.utc)
    now_iso = _iso_z(now)
    cutoff = now.timestamp() - max_age_hours * 3600
    kept = []
    for rec in state.get("alerted", []):
        try:
            if datetime.fromisoformat(str(rec.get("ts", ""))).timestamp() >= cutoff:
                kept.append(rec)
        except ValueError:
            pass
    if len(kept) != len(state.get("alerted", [])):
        state["alerted"] = kept
        _write_state(state)

    since_iso = state.get("last_watchman_run") or _iso_z(
        now - timedelta(minutes=fallback_minutes))
    today = now.date().isoformat()
    alerts_today = state.get("alerts_today") or {}
    if alerts_today.get("date") != today:
        alerts_today = {"date": today, "count": 0}

    queued = state.get("queued_for_digest", [])
    queued_lines = [f"{q.get('id')} | {str(q.get('topic') or q.get('headline') or '')[:110]}"
                    for q in queued[-WATCHMAN_QUEUE_LINES:]]
    out: dict[str, Any] = {
        "now_iso": now_iso,
        "since_iso": since_iso,
        "alerts_today": alerts_today,
        "alerted_24h": [{k: r.get(k) for k in ("topic_key", "headline", "ts")}
                        for r in kept],
        "queued_total": len(queued),
        "queued_recent": "\n".join(queued_lines),
    }
    data = _load_clusters()
    if not data:
        out["error"] = "clusters.json not found — clusterer has not run yet; fall back to list_news"
        return out
    clusters = [c for c in data.get("clusters", [])
                if str(c.get("last_ts") or "") >= since_iso]
    page = clusters[:limit]
    out.update({
        "clusters_generated_at": data.get("generated_at"),
        "clusters_count": len(page),
        "window_total": len(clusters),
        "truncated": len(clusters) > len(page),
        "legend": _WATCHMAN_LEGEND,
        "clusters": "\n".join(_cluster_line(c, {}, {}, full=True) for c in page),
    })
    return out


@mcp.tool()
def watchman_finish(
    log_text: str,
    queued: list[dict] | None = None,
    alerts: list[dict] | None = None,
) -> dict:
    """
    Close the watchman run in ONE call (replaces send_telegram ×N +
    send_telegram_comment + update_state):
      1. for each alert: post it to the main channel, post its methodology
         comment under it, remember both message ids;
      2. post the decisions log to the log channel;
      3. patch state: append queued, append alerted (with channel_msg_id /
         discussion_msg_id for the feedback collector), bump alerts_today,
         set last_watchman_run = now.

    Args:
      log_text  — the «📋 Watchman …» decisions log (Markdown, [[art_id]]
                  placeholders allowed). Sent even when there are no alerts.
      queued[]  — [{id: art_…, topic: "…"}] new items for the digest queue
                  (id = cluster id = its first article id). [] when none.
      alerts[]  — [{topic_key, headline, text, methodology, article_ids}]:
                  text = the 🚨 BREAKING message itself (Markdown), methodology
                  = the «🤖 Почему это попало в BREAKING» comment (Markdown),
                  article_ids = 2–4 ids of DIFFERENT outlets used in text.
                  [] when nothing passed the four filters (the normal case).
    Telegram parse errors (Markdown 400 «can't parse entities») are retried
    as HTML automatically. An alert whose main message failed is NOT
    recorded (retry next run); a failed comment leaves discussion_msg_id
    null. Returns a compact summary, never the whole state.
    """
    now = datetime.now(timezone.utc)
    ts = _iso_z(now)
    errors: list[dict] = []
    records: list[dict] = []
    for a in alerts or []:
        text = str(a.get("text") or "").strip()
        if not text:
            errors.append({"alert": a.get("topic_key"), "error": "empty text"})
            continue
        sent = _send_telegram_with_fallback(text, target="main")
        msg_id = _first_message_id(sent)
        if msg_id is None or not _tg_all_ok(sent):
            errors.append({"alert": a.get("topic_key"), "error": "alert not sent",
                           "telegram": sent.get("results")})
            continue
        discussion_msg_id = None
        if a.get("methodology"):
            c = _send_comment_with_fallback(str(a["methodology"]), msg_id)
            if c.get("error") or not _tg_all_ok(c):
                errors.append({"alert": a.get("topic_key"), "error": "comment failed",
                               "detail": c.get("error") or c.get("results")})
            else:
                discussion_msg_id = c.get("discussion_msg_id")
        records.append({
            "topic_key": a.get("topic_key"),
            "headline": a.get("headline"),
            "ts": ts,
            "article_ids": list(a.get("article_ids") or []),
            "channel_msg_id": msg_id,
            "discussion_msg_id": discussion_msg_id,
        })

    log = _send_telegram_with_fallback(log_text, target="log", disable_preview=True)
    if not _tg_all_ok(log):
        errors.append({"log": "not sent", "telegram": log.get("results")})

    new_queued = [{"id": q.get("id"), "topic": q.get("topic") or q.get("headline")}
                  for q in (queued or []) if q.get("id")]
    patch: dict[str, Any] = {"last_watchman_run": ts}
    if new_queued:
        patch["append_queued"] = new_queued
    if records:
        patch["append_alerted"] = records
        patch["increment_alerts_today"] = len(records)
    st = _apply_state_patch(patch)
    if st.get("error"):
        errors.append({"state": st["error"]})
    return {
        "ts": ts,
        "alerts_sent": len(records),
        "alert_msg_ids": [r["channel_msg_id"] for r in records],
        "queued_added": len(new_queued),
        "queued_total": len(st.get("queued_for_digest") or []),
        "alerts_today": st.get("alerts_today"),
        "log_sent": _tg_all_ok(log),
        "last_watchman_run": st.get("last_watchman_run"),
        "errors": errors,
    }


# ────────────────────── Entrypoint ──────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--http",
        action="store_true",
        help="serve streamable HTTP with bearer auth (for cloud routines "
             "via tunnel) instead of stdio",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()

    if args.http:
        from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

        token = _load_config().get("mcp_bearer_token")
        if not token:
            sys.exit(
                "mcp_bearer_token missing in config.json — generate one: "
                "python -c \"import secrets; print(secrets.token_urlsafe(32))\""
            )
        mcp.auth = StaticTokenVerifier(
            tokens={token: {"client_id": "newday-remote", "scopes": []}}
        )
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()