"""
Deterministic news fetcher.

Reads sources.txt, fetches RSS, dedupes, writes inbox.json.
Old articles (>72h) get rolled out to archive.jsonl.

Run by Task Scheduler every 15-30 minutes.

Usage:
    python fetcher.py [--root D:\\newDay]
"""
from __future__ import annotations

import argparse
import email.utils as eut
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import feedparser
import requests

import doh
from doh import install_global_doh


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
INBOX_HORIZON_HOURS = 72         # how long an article stays in inbox.json
DEDUP_TITLE_PREFIX_LEN = 90      # chars from title used for cross-source dedup
REQUEST_TIMEOUT = 20


# ───────────────────────── Data model ─────────────────────────

@dataclass
class Article:
    id: str
    source: str
    title: str
    summary: str
    url: str
    published: str    # ISO UTC

    @property
    def title_key(self) -> str:
        # normalised title prefix for cross-source dedup
        s = re.sub(r"\s+", " ", self.title.lower()).strip()
        return s[:DEDUP_TITLE_PREFIX_LEN]


@dataclass
class SourceStats:
    entries_received: int = 0
    dated_with_fallback: int = 0
    dropped_too_old: int = 0
    dropped_dup_url: int = 0
    dropped_dup_title: int = 0
    added_new: int = 0
    error: str | None = None
    health: str = "ok"


# ───────────────────────── Helpers ─────────────────────────

def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", file=sys.stderr)


def parse_date(entry) -> datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        t = entry.get(key)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                continue
    for key in ("dc_date", "date"):
        s = entry.get(key)
        if s:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except (TypeError, ValueError):
                pass
    raw = entry.get("published") or entry.get("updated") or entry.get("pubDate")
    if raw:
        try:
            dt = eut.parsedate_to_datetime(raw)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (TypeError, ValueError):
            pass
    return None


def health_verdict(s: SourceStats) -> str:
    if s.error:
        return "fetch_error"
    if s.entries_received == 0:
        return "no_entries"
    if s.dated_with_fallback == s.entries_received:
        # every entry lacked a parseable date — articles flow with fetch-time
        # fallback, but the feed is degraded
        return "no_dates"
    if s.added_new == 0:
        kept = s.entries_received - s.dropped_too_old
        if s.dropped_too_old == s.entries_received:
            return "all_too_old"
        if kept > 0 and (s.dropped_dup_url + s.dropped_dup_title) >= kept:
            return "all_duplicate"
    return "ok"


def make_id(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]
    return f"art_{h}"


def read_sources(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "|" not in line:
            log(f"  [skip] malformed line: {line!r}")
            continue
        name, url = (p.strip() for p in line.split("|", 1))
        if name and url:
            out.append((name, url))
    return out


def fetch_feed(url: str) -> tuple[bytes | None, str | None]:
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5",
            },
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"
        return r.content, None
    except requests.RequestException as e:
        return None, str(e)


def parse_articles(
    source: str, raw: bytes, cutoff: datetime, stats: SourceStats
) -> Iterable[Article]:
    feed = feedparser.parse(raw)
    entries = feed.entries[:50]
    stats.entries_received = len(entries)
    fallback_now = datetime.now(timezone.utc)
    for entry in entries:
        url = (entry.get("link") or "").strip()
        title = (entry.get("title") or "").strip()
        if not url or not title:
            continue
        pub = parse_date(entry)
        if pub is None:
            # Date-less feed (e.g. The Baltic Times). Stamp with fetch-time so
            # the article still flows; track it so the source's health verdict
            # can flag the degradation.
            pub = fallback_now
            stats.dated_with_fallback += 1
        elif pub < cutoff:
            stats.dropped_too_old += 1
            continue
        summary = re.sub(r"<[^>]+>", " ", entry.get("summary", "") or "")
        summary = re.sub(r"\s+", " ", summary).strip()[:500]
        yield Article(
            id=make_id(url),
            source=source,
            title=title,
            summary=summary,
            url=url,
            published=pub.isoformat(),
        )


# ───────────────────────── Inbox / archive I/O ─────────────────────────

def load_inbox(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("articles", []) if isinstance(data, dict) else []
    except (json.JSONDecodeError, OSError):
        return []


def write_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(tmp, path)


def append_archive(path: Path, articles: list[dict]) -> None:
    if not articles:
        return
    with path.open("a", encoding="utf-8") as f:
        for a in articles:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")


# ───────────────────────── Main ─────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()

    if install_global_doh():
        log(f"DoH active (all hosts via {doh._DOH_HOST})")
    else:
        log("DoH unavailable — falling back to system DNS")

    root: Path = args.root
    sources_path = root / "sources.txt"
    inbox_path = root / "inbox.json"
    archive_path = root / "archive.jsonl"
    fetch_log_path = root / "fetch_errors.json"

    if not sources_path.exists():
        log(f"sources.txt not found at {sources_path}")
        return 2

    cutoff = datetime.now(timezone.utc) - timedelta(hours=INBOX_HORIZON_HOURS)
    sources = read_sources(sources_path)
    log(f"Sources: {len(sources)}")

    by_url: dict[str, Article] = {}
    by_title: dict[str, Article] = {}
    errors: list[dict] = []
    counts: dict[str, int] = {}
    per_source: dict[str, SourceStats] = {}

    # 1. Carry over existing inbox so we don't lose articles between runs.
    for art in load_inbox(inbox_path):
        try:
            a = Article(**art)
        except TypeError:
            continue
        pub_dt = datetime.fromisoformat(a.published)
        if pub_dt >= cutoff:
            by_url[a.url] = a
            by_title.setdefault(a.title_key, a)

    # 2. Fetch fresh articles from each source.
    for name, url in sources:
        stats = SourceStats()
        per_source[name] = stats
        raw, err = fetch_feed(url)
        if err:
            log(f"  ✗ {name}: {err}")
            errors.append({"source": name, "url": url, "error": err})
            stats.error = err
            stats.health = health_verdict(stats)
            counts[name] = 0
            continue

        for art in parse_articles(name, raw, cutoff, stats):
            if art.url in by_url:
                stats.dropped_dup_url += 1
                continue
            # cross-source dedup: same headline from another outlet
            existing = by_title.get(art.title_key)
            if existing and existing.source != art.source:
                stats.dropped_dup_title += 1
                continue
            by_url[art.url] = art
            by_title.setdefault(art.title_key, art)
            stats.added_new += 1
        counts[name] = stats.added_new
        stats.health = health_verdict(stats)
        log(f"  ✓ {name}: +{stats.added_new} [{stats.health}]")

    # 3. Sort by publish time (newest first), reassign nothing — ids stay stable.
    articles = sorted(
        by_url.values(),
        key=lambda a: a.published,
        reverse=True,
    )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "horizon_hours": INBOX_HORIZON_HOURS,
        "count": len(articles),
        "counts_by_source": counts,
        "articles": [asdict(a) for a in articles],
    }
    write_atomic(inbox_path, payload)

    # Persist the fetch error report separately so the MCP / prompt can read it.
    # `dead_sources` keeps its old name for back-compat with prompts/digest.md
    # but is now restricted to sources that are genuinely broken — not just
    # quiet or fully deduped.
    truly_broken = {"fetch_error", "no_entries", "all_too_old"}
    write_atomic(
        fetch_log_path,
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "errors": errors,
            "ok_sources": sum(
                1 for s in per_source.values() if s.health == "ok"
            ),
            "dead_sources": sorted(
                name for name, s in per_source.items()
                if s.health in truly_broken
            ),
            "per_source": {
                name: asdict(s) for name, s in per_source.items()
            },
        },
    )

    # 4. Roll out anything older than horizon to archive.
    expired: list[dict] = []
    for art_dict in load_inbox(inbox_path):  # re-read so we work on persisted data
        try:
            pub_dt = datetime.fromisoformat(art_dict["published"])
        except (KeyError, ValueError):
            continue
        if pub_dt < cutoff:
            expired.append(art_dict)
    append_archive(archive_path, expired)

    log(f"Wrote {len(articles)} articles · {len(errors)} errors · "
        f"archived {len(expired)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())