"""
Deterministic event clusterer.

Reads inbox.json, embeds new articles (multilingual-e5-base, CPU only),
clusters the 72h window into events, writes clusters.json atomically.

Recipe validated in experiments/cluster_lab.py (2026-07-16):
  - base pass: incremental clustering in published order, cosine >= 0.90
    against BOTH the cluster centroid and its seed article (anti-drift);
  - split pass: any cluster > 30 articles is re-clustered internally at
    0.93 — separates real sub-events from the daily war-sludge blob.

Cluster ids are the article id of the cluster seed (stable across runs
for as long as the seed stays in the 72h window). Full re-cluster each
run: deterministic given the same inbox, no incremental state to corrupt.

Embedding vectors are cached in cache/embeddings.npz keyed by article id
(an article is embedded exactly once in its lifetime; the cache is pruned
to the current inbox on every run).

GPU is intentionally NOT used: this is a gaming machine. CPU handles the
regular increment (~30-100 new articles) in seconds.

Run after fetcher.py (see scripts/run-fetcher.ps1).

Usage:
    python clusterer.py [--root D:\\newDay]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# The model is downloaded once into the HF cache; scheduled runs must not
# ping the HF Hub (update checks, unauthenticated-rate-limit warnings) or
# spam progress bars into the task log. setdefault so a manual
# `HF_HUB_OFFLINE=0 python clusterer.py` can still re-download the model.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")

import numpy as np

MODEL_NAME = "intfloat/multilingual-e5-base"
BASE_THRESHOLD = 0.90
SEED_MARGIN = 0.02          # seed check runs at BASE_THRESHOLD - SEED_MARGIN
SPLIT_SIZE = 30             # clusters larger than this get an inner re-pass
SPLIT_THRESHOLD = 0.93
TOP_CANDIDATES = 5          # centroid candidates checked against the seed
RELATED_THRESHOLD = 0.85    # centroid similarity that links twin clusters
RELATED_MAX = 5             # related_ids a cluster picks for itself
RELATED_HARD_CAP = 10       # incl. symmetrized back-links: hub clusters
                            # (war, EU-Ukraine) otherwise accumulate 100+

# Language zone per source — carried into clusters.json so prompts can see
# coverage without knowing source names. Keep in sync with sources.txt.
SOURCE_LANG = {
    "BBC World": "en", "Guardian World": "en", "NYT World": "en",
    "Al Jazeera": "en", "TechCrunch": "en", "Hacker News": "en",
    "Ars Technica": "en", "The Verge": "en", "DW English": "en",
    "France24": "en", "ERR News": "en", "Postimees EN": "en",
    "The Baltic Times": "en", "hongkongnews.net": "en", "HK HKFP": "en",
    "CGTN World": "en", "SCMP China": "en", "Times of Israel": "en",
    "Anadolu World": "en", "Hindu Intl": "en",
    "MarketWatch": "en", "CNBC Markets": "en", "FT": "en",
    "Meduza": "ru", "RIA": "ru", "TASS": "ru", "ERR rus": "ru",
    "Postimees RU": "ru", "Unian RU": "ru", "Pravda RU": "ru",
    "ERR est": "et", "Postimees EE": "et",
    "Pravda UA": "uk",
}

# Feeds that are the same newsroom in different languages. Clusters report
# distinct *outlets*, so Pravda RU + Pravda UA no longer pass for two
# independent sources in watchman's confirmation filter.
SOURCE_OUTLET = {
    "ERR News": "ERR", "ERR rus": "ERR", "ERR est": "ERR",
    "Postimees EN": "Postimees", "Postimees RU": "Postimees",
    "Postimees EE": "Postimees",
    "Pravda RU": "Pravda", "Pravda UA": "Pravda",
}


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}",
          file=sys.stderr)


def clean(text: str | None) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def article_text(a: dict) -> str:
    title = clean(a.get("title"))
    summary = clean(a.get("summary"))[:400]
    # e5 models expect a "query: " prefix for symmetric similarity
    return f"query: {title}. {summary}" if summary else f"query: {title}"


def load_inbox(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    arts = data.get("articles", []) if isinstance(data, dict) else []
    arts = [a for a in arts if a.get("id") and a.get("title")]
    # stable stream order: published, then id as tie-breaker
    arts.sort(key=lambda a: (a.get("published") or "", a["id"]))
    return arts


def load_cache(path: Path) -> dict[str, np.ndarray]:
    if not path.exists():
        return {}
    try:
        z = np.load(path, allow_pickle=True)
        return dict(zip(z["ids"].tolist(), z["vecs"]))
    except Exception as e:
        log(f"embedding cache unreadable ({e}) — rebuilding from scratch")
        return {}


def save_cache(path: Path, cache: dict[str, np.ndarray]) -> None:
    ids = list(cache.keys())
    tmp = path.with_suffix(".tmp.npz")
    np.savez_compressed(tmp, ids=np.array(ids, dtype=object),
                        vecs=np.stack([cache[i] for i in ids]))
    os.replace(tmp, path)


def embed_missing(arts: list[dict], cache: dict[str, np.ndarray]) -> int:
    todo = [a for a in arts if a["id"] not in cache]
    if not todo:
        return 0
    # heavy import deferred: runs that add nothing skip torch entirely
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(MODEL_NAME, device="cpu")
    vecs = model.encode([article_text(a) for a in todo], batch_size=64,
                        normalize_embeddings=True, show_progress_bar=False)
    for a, v in zip(todo, vecs):
        cache[a["id"]] = v.astype(np.float32)
    return len(todo)


def cluster_pass(indices: list[int], vecs: list[np.ndarray],
                 threshold: float) -> list[list[int]]:
    """Incremental clustering with an anti-drift seed anchor.

    A candidate joins a cluster only if it is similar to the centroid AND
    to the cluster's first article — the cluster cannot crawl away from
    the event it started as.
    """
    centroids: list[np.ndarray] = []
    seeds: list[np.ndarray] = []
    sums: list[np.ndarray] = []
    members: list[list[int]] = []
    for i in indices:
        v = vecs[i]
        placed = False
        if centroids:
            sims = np.stack(centroids) @ v
            for j in np.argsort(sims)[::-1][:TOP_CANDIDATES]:
                if sims[j] < threshold:
                    break
                if seeds[j] @ v >= threshold - SEED_MARGIN:
                    members[j].append(i)
                    sums[j] = sums[j] + v
                    centroids[j] = sums[j] / np.linalg.norm(sums[j])
                    placed = True
                    break
        if not placed:
            centroids.append(v)
            seeds.append(v.copy())
            sums.append(v.copy())
            members.append([i])
    return members


def build_clusters(arts: list[dict],
                   cache: dict[str, np.ndarray]) -> list[dict]:
    vecs = [cache[a["id"]] for a in arts]
    base = cluster_pass(list(range(len(arts))), vecs, BASE_THRESHOLD)

    events: list[tuple[list[int], str | None]] = []  # (members, storyline_id)
    for m in base:
        if len(m) <= SPLIT_SIZE:
            events.append((m, None))
            continue
        # storyline blob (e.g. daily war sludge): split into sub-events,
        # keep the blob's identity as storyline_id on every child
        storyline_id = arts[m[0]]["id"]
        for sub in cluster_pass(m, vecs, SPLIT_THRESHOLD):
            events.append((sub, storyline_id))

    out = []
    for m, storyline_id in events:
        members = [arts[i] for i in m]
        sources = sorted({a["source"] for a in members})
        outlets = sorted({SOURCE_OUTLET.get(s, s) for s in sources})
        langs = sorted({SOURCE_LANG.get(s, "?") for s in sources})
        # up to 3 citation-ready ids from DISTINCT outlets. The cluster id
        # alone is the EARLIEST article — usually a newswire (TASS), so
        # citing only it systematically over-credits the fastest source.
        sample, seen = [], set()
        for a in members:
            o = SOURCE_OUTLET.get(a["source"], a["source"])
            if o not in seen:
                seen.add(o)
                sample.append(a["id"])
            if len(sample) == 3:
                break
        out.append({
            "id": members[0]["id"],           # seed article id — stable
            "size": len(members),
            "sources": sources,
            "outlets": outlets,               # independent newsrooms
            "langs": langs,
            "first_ts": members[0].get("published"),
            "last_ts": members[-1].get("published"),
            "title": clean(members[0].get("title"))[:200],
            "sample_ids": sample,
            "article_ids": [a["id"] for a in members],
            "storyline_id": storyline_id,     # null unless from a split blob
            "related_ids": [],
        })
    link_related(out, vecs, [m for m, _ in events])
    # most-covered events first
    out.sort(key=lambda c: (len(c["outlets"]), c["size"]), reverse=True)
    return out


def link_related(clusters: list[dict], vecs: list[np.ndarray],
                 events: list[list[int]]) -> None:
    """Cross-link twin clusters of the same event.

    en<->ru pairs merge worse than ru<->uk/et at BASE_THRESHOLD, so one
    event often yields two monolingual clusters. We don't lower the
    threshold (that brings over-merge back) — instead clusters whose
    centroids are similar above RELATED_THRESHOLD point at each other via
    related_ids. For delta reading that's better than a merge: "here is
    the western version, here is the ru one" comes pre-paired.

    Only clusters with >= 2 articles take part: linking singleton noise
    to everything would drown the signal.
    """
    idx = [i for i, m in enumerate(events) if len(m) >= 2]
    if len(idx) < 2:
        return
    cents = np.stack([
        (c := sum(vecs[j] for j in events[i])) / np.linalg.norm(c)
        for i in idx
    ])
    sims = cents @ cents.T
    np.fill_diagonal(sims, 0.0)
    for row, i in enumerate(idx):
        my_langs = set(clusters[i]["langs"])
        near = [col for col in np.argsort(sims[row])[::-1]
                if sims[row][col] >= RELATED_THRESHOLD]
        # cross-language similarity is systematically lower than
        # same-language, so monolingual topical neighbours would crowd the
        # actual other-language twin out of the top — reserve slots for
        # candidates that add a language zone we don't have
        cross = [c for c in near
                 if set(clusters[idx[c]]["langs"]) - my_langs][:2]
        picked = list(dict.fromkeys(cross + near))[:RELATED_MAX]
        picked.sort(key=lambda c: sims[row][c], reverse=True)
        clusters[i]["related_ids"] = [clusters[idx[c]]["id"] for c in picked]
    # symmetrize: if A points at B, B points back at A — the relation is
    # mutual by construction, only the top-N cut made it one-sided.
    # Hard-capped: popular hub clusters would otherwise collect a
    # back-link from every neighbour and balloon to 100+ entries.
    by_id = {c["id"]: c for c in clusters}
    for i in idx:
        for rid in clusters[i]["related_ids"][:RELATED_MAX]:
            other = by_id[rid]
            if (clusters[i]["id"] not in other["related_ids"]
                    and len(other["related_ids"]) < RELATED_HARD_CAP):
                other["related_ids"].append(clusters[i]["id"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).parent)
    args = parser.parse_args()
    root: Path = args.root

    inbox_path = root / "inbox.json"
    cache_path = root / "cache" / "embeddings.npz"
    out_path = root / "clusters.json"

    if not inbox_path.exists():
        log("inbox.json not found — nothing to cluster")
        return 2

    arts = load_inbox(inbox_path)
    log(f"articles in window: {len(arts)}")

    cache = load_cache(cache_path)
    new = embed_missing(arts, cache)
    # prune vectors for articles that left the 72h window
    live_ids = {a["id"] for a in arts}
    cache = {k: v for k, v in cache.items() if k in live_ids}
    save_cache(cache_path, cache)
    log(f"embedded {new} new · cache {len(cache)} vectors")

    clusters = build_clusters(arts, cache)
    multi = sum(1 for c in clusters if len(c["outlets"]) >= 2)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": MODEL_NAME,
        "params": {
            "base_threshold": BASE_THRESHOLD,
            "split_size": SPLIT_SIZE,
            "split_threshold": SPLIT_THRESHOLD,
        },
        "article_count": len(arts),
        "cluster_count": len(clusters),
        "multi_outlet_count": multi,
        "clusters": clusters,
    }
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, out_path)
    log(f"wrote {len(clusters)} clusters ({multi} multi-outlet) -> {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
