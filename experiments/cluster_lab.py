# -*- coding: utf-8 -*-
r"""
Лабораторный скрипт: кластеризация inbox.json по событиям через
мультиязычные эмбеддинги. НЕ трогает боевой пайплайн.

Запуск:
    python experiments\cluster_lab.py                 # эмбеддинг (с кешем) + кластеризация
    python experiments\cluster_lab.py --threshold 0.80
    python experiments\cluster_lab.py --model intfloat/multilingual-e5-large

Эмбеддинги кешируются в experiments\emb_cache_<model>.npz — смена порога
пересчёта не требует.

Отчёт: experiments\report_<threshold>.md — смотреть глазами.
"""
import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone

import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INBOX = os.path.join(BASE, "inbox.json")
EXP = os.path.dirname(os.path.abspath(__file__))

# Языковая зона источника — только для отчёта (проверка кросс-языковой склейки)
SOURCE_LANG = {
    "BBC World": "en", "Guardian World": "en", "NYT World": "en",
    "Al Jazeera": "en", "TechCrunch": "en", "Hacker News": "en",
    "Ars Technica": "en", "The Verge": "en", "DW English": "en",
    "France24": "en", "ERR News": "en", "Postimees EN": "en",
    "The Baltic Times": "en", "hongkongnews.net": "en", "HK HKFP": "en",
    "Meduza": "ru", "RIA": "ru", "TASS": "ru", "ERR rus": "ru",
    "Postimees RU": "ru", "Unian RU": "ru", "Pravda RU": "ru",
    "ERR est": "et", "Postimees EE": "et",
    "Pravda UA": "uk",
}


def load_articles():
    with open(INBOX, encoding="utf-8") as f:
        data = json.load(f)
    arts = [x for x in (data if isinstance(data, list) else data.get("articles", []))
            if isinstance(x, dict) and x.get("title")]
    # стабильный порядок по времени публикации — имитируем поток
    arts.sort(key=lambda a: a.get("published") or "")
    return arts


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def article_text(a):
    title = clean(a.get("title"))
    summary = clean(a.get("summary"))[:400]
    return f"{title}. {summary}" if summary else title


def get_embeddings(arts, model_name):
    safe = model_name.replace("/", "_")
    cache_path = os.path.join(EXP, f"emb_cache_{safe}.npz")
    cached = {}
    if os.path.exists(cache_path):
        z = np.load(cache_path, allow_pickle=True)
        cached = dict(zip(z["ids"].tolist(), z["vecs"]))
        print(f"cache: {len(cached)} векторов из {cache_path}")

    todo = [a for a in arts if a["id"] not in cached]
    if todo:
        from sentence_transformers import SentenceTransformer
        print(f"эмбеддинг {len(todo)} новых статей моделью {model_name} (CPU)...")
        t0 = time.time()
        model = SentenceTransformer(model_name, device="cpu")
        prefix = "query: " if "e5" in model_name.lower() else ""
        texts = [prefix + article_text(a) for a in todo]
        vecs = model.encode(texts, batch_size=64, normalize_embeddings=True,
                            show_progress_bar=True)
        for a, v in zip(todo, vecs):
            cached[a["id"]] = v.astype(np.float32)
        print(f"готово за {time.time()-t0:.0f} c "
              f"({len(todo)/(time.time()-t0):.0f} статей/с)")
        ids = list(cached.keys())
        np.savez_compressed(cache_path, ids=np.array(ids, dtype=object),
                            vecs=np.stack([cached[i] for i in ids]))
    return cached


def cluster(arts, emb, threshold, seed_margin=0.02):
    """Инкрементальная кластеризация в порядке публикации.

    Против дрейфа центроида: кандидат обязан быть похож не только на
    центроид, но и на статью-основателя кластера (seed). Кластер не
    может уползти от события, с которого начался.
    """
    centroids = []          # нормированные центроиды
    seeds = []              # вектор первой статьи кластера
    members = []            # индексы статей
    sums = []               # ненормированная сумма векторов
    for i, a in enumerate(arts):
        v = emb[a["id"]]
        placed = False
        if centroids:
            C = np.stack(centroids)
            sims = C @ v
            # кандидаты по центроиду, лучшие сначала
            for j in np.argsort(sims)[::-1][:5]:
                if sims[j] < threshold:
                    break
                if seeds[j] @ v >= threshold - seed_margin:
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


def report(arts, members, threshold, model_name):
    sizes = Counter(len(m) for m in members)
    multi = [m for m in members if len(m) >= 2]

    def srcs(m):
        return {arts[i].get("source", "?") for i in m}

    def langs(m):
        return {SOURCE_LANG.get(arts[i].get("source"), "?") for i in m}

    multi_source = [m for m in multi if len(srcs(m)) >= 2]
    cross_lang = [m for m in multi_source if len(langs(m)) >= 2]

    lines = [
        f"# Отчёт кластеризации — threshold={threshold}, model={model_name}",
        f"Статей: {len(arts)} · кластеров: {len(members)} · "
        f"синглтонов: {sizes[1]} ({sizes[1]*100//len(members)}%)",
        f"Кластеров с ≥2 статьями: {len(multi)} · с ≥2 источниками: "
        f"{len(multi_source)} · кросс-языковых: {len(cross_lang)}",
        "",
        "## Топ-40 кластеров по разнообразию источников",
        "(проверять: 1) не слиплись ли РАЗНЫЕ события 2) склеились ли языки)",
        "",
    ]
    ranked = sorted(members, key=lambda m: (len(srcs(m)), len(m)), reverse=True)
    for n, m in enumerate(ranked[:40], 1):
        s, lg = srcs(m), langs(m)
        first = arts[m[0]].get("published", "")[:16]
        last = arts[m[-1]].get("published", "")[:16]
        lines.append(f"### {n}. {len(m)} статей · {len(s)} источников · "
                     f"языки: {','.join(sorted(lg))} · {first} → {last}")
        for i in m[:14]:
            a = arts[i]
            lines.append(f"- [{a.get('source','?')}] {clean(a.get('title'))[:130]}")
        if len(m) > 14:
            lines.append(f"- ... ещё {len(m)-14}")
        lines.append("")

    lines.append("## Случайные кластеры из 2-4 статей (проверка на ложную склейку)")
    lines.append("")
    small = [m for m in multi_source if 2 <= len(m) <= 4]
    rng = np.random.RandomState(42)
    for m in [small[k] for k in rng.permutation(len(small))[:25]]:
        lines.append(f"### пара/тройка · языки: {','.join(sorted(langs(m)))}")
        for i in m:
            a = arts[i]
            lines.append(f"- [{a.get('source','?')}] {clean(a.get('title'))[:130]}")
        lines.append("")

    out = os.path.join(EXP, f"report_{str(threshold).replace('.','')}.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"отчёт: {out}")
    print(f"итого: {len(members)} кластеров, синглтонов {sizes[1]}, "
          f"multi-source {len(multi_source)}, cross-lang {len(cross_lang)}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default="intfloat/multilingual-e5-base")
    p.add_argument("--threshold", type=float, default=0.86)
    args = p.parse_args()

    arts = load_articles()
    print(f"статей: {len(arts)}")
    emb = get_embeddings(arts, args.model)
    members = cluster(arts, emb, args.threshold)
    report(arts, members, args.threshold, args.model)


if __name__ == "__main__":
    main()
