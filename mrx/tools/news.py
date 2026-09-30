"""Live global news: RSS from many countries/languages + GDELT (worldwide, 100+ languages, updated every 15 min)."""
from __future__ import annotations

import asyncio
import calendar
import time
from typing import Any

import feedparser
import httpx

from .registry import Context, ToolError, i, s, tool

UA = {"User-Agent": "MRX-agent/0.1 (+personal news reader)"}

FEEDS: dict[str, dict[str, list[tuple[str, str]]]] = {
    "en": {
        "world": [("BBC", "http://feeds.bbci.co.uk/news/world/rss.xml"), ("Al Jazeera", "https://www.aljazeera.com/xml/rss/all.xml"),
                  ("Guardian", "https://www.theguardian.com/world/rss"), ("DW", "https://rss.dw.com/rdf/rss-en-all"),
                  ("France 24", "https://www.france24.com/en/rss"), ("NPR", "https://feeds.npr.org/1004/rss.xml"),
                  ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"), ("The Hindu", "https://www.thehindu.com/news/international/feed/")],
        "technology": [("BBC Tech", "http://feeds.bbci.co.uk/news/technology/rss.xml"), ("Ars Technica", "https://feeds.arstechnica.com/arstechnica/index"),
                       ("The Verge", "https://www.theverge.com/rss/index.xml")],
        "business": [("BBC Business", "http://feeds.bbci.co.uk/news/business/rss.xml"), ("Guardian Business", "https://www.theguardian.com/uk/business/rss")],
        "science": [("BBC Science", "http://feeds.bbci.co.uk/news/science_and_environment/rss.xml"), ("NASA", "https://www.nasa.gov/rss/dyn/breaking_news.rss")],
        "sports": [("BBC Sport", "http://feeds.bbci.co.uk/sport/rss.xml")],
        "health": [("BBC Health", "http://feeds.bbci.co.uk/news/health/rss.xml")],
    },
    "de": {"world": [("Tagesschau", "https://www.tagesschau.de/index~rss2.xml"), ("DW Deutsch", "https://rss.dw.com/rdf/rss-de-all")]},
    "fr": {"world": [("France 24 FR", "https://www.france24.com/fr/rss"), ("Le Monde", "https://www.lemonde.fr/rss/une.xml")]},
    "es": {"world": [("El País", "https://feeds.elpais.com/mrss-s/pages/ep/site/elpais.com/portada"), ("BBC Mundo", "https://feeds.bbci.co.uk/mundo/rss.xml")]},
    "ar": {"world": [("Al Jazeera AR", "https://www.aljazeera.net/aljazeera/rss"), ("BBC Arabic", "https://feeds.bbci.co.uk/arabic/rss.xml")]},
    "hi": {"world": [("BBC Hindi", "https://feeds.bbci.co.uk/hindi/rss.xml")]},
    "ja": {"world": [("NHK", "https://www3.nhk.or.jp/rss/news/cat0.xml")]},
    "zh": {"world": [("BBC 中文", "https://feeds.bbci.co.uk/zhongwen/simp/rss.xml")]},
    "ru": {"world": [("BBC Russian", "https://feeds.bbci.co.uk/russian/rss.xml")]},
    "pt": {"world": [("BBC Brasil", "https://feeds.bbci.co.uk/portuguese/rss.xml")]},
}

_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 120


def _cached(key: str) -> Any | None:
    hit = _cache.get(key)
    return hit[1] if hit and time.time() - hit[0] < CACHE_TTL else None


async def _fetch_feed(client: httpx.AsyncClient, source: str, url: str) -> tuple[list[dict], str | None]:
    try:
        r = await client.get(url, headers=UA, follow_redirects=True)
        r.raise_for_status()
    except Exception as e:  # noqa: BLE001
        return [], f"{source}: {type(e).__name__}"
    parsed = feedparser.parse(r.content)
    items = []
    for e in parsed.entries[:25]:
        ts = e.get("published_parsed") or e.get("updated_parsed")
        items.append({"source": source, "title": (e.get("title") or "").strip(), "url": e.get("link"),
                      "summary": _clean(e.get("summary", ""))[:300], "time": calendar.timegm(ts) if ts else 0})
    return items, None


def _clean(html: str) -> str:
    from .email_tools import _html_to_text  # noqa: PLC0415
    return _html_to_text(html)


async def fetch_headlines(topic: str = "world", lang: str = "en", limit: int = 25) -> dict:
    lang = lang.lower()[:2] or "en"
    topic = topic.lower()
    feeds = FEEDS.get(lang, {}).get(topic) or FEEDS.get(lang, {}).get("world") or FEEDS["en"].get(topic) or FEEDS["en"]["world"]
    key = f"rss:{lang}:{topic}"
    if (hit := _cached(key)) is None:
        async with httpx.AsyncClient(timeout=12) as client:
            results = await asyncio.gather(*(_fetch_feed(client, name, url) for name, url in feeds))
        items = sorted((it for r, _ in results for it in r), key=lambda x: x["time"], reverse=True)
        errors = [e for _, e in results if e]
        hit = {"items": items, "errors": errors}
        if items:
            _cache[key] = (time.time(), hit)
    seen, unique = set(), []
    for it in hit["items"]:
        k = it["title"].lower()[:60]
        if k and k not in seen:
            seen.add(k)
            unique.append(it)
    return {"topic": topic, "lang": lang, "items": unique[:limit], "failed_sources": hit["errors"]}


async def gdelt_articles(query: str, timespan: str = "1d", limit: int = 20, lang: str = "") -> list[dict]:
    q = query + (f" sourcelang:{lang}" if lang else "")
    params = {"query": q, "mode": "ArtList", "format": "json", "maxrecords": str(min(limit, 75)), "sort": "DateDesc", "timespan": timespan}
    key = "gdelt:" + repr(sorted(params.items()))
    if (hit := _cached(key)) is not None:
        return hit
    async with httpx.AsyncClient(timeout=25) as client:
        r = await client.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, headers=UA)
    if r.status_code == 429:
        raise ToolError("GDELT is rate-limiting (1 request / 5 s). Try again in a few seconds.")
    try:
        data = r.json()
    except ValueError:
        raise ToolError(f"GDELT returned no results ({r.text[:120]!r}). Try simpler keywords.")
    items = [{"source": a.get("domain"), "title": a.get("title"), "url": a.get("url"), "language": a.get("language"),
              "country": a.get("sourcecountry"), "time": a.get("seendate")} for a in data.get("articles", [])]
    _cache[key] = (time.time(), items)
    return items


@tool("news_headlines", "Get live headlines from major outlets worldwide. Topics: world, technology, business, science, sports, health. "
                        "lang picks native-language outlets (en, de, fr, es, ar, hi, ja, zh, ru, pt). Also shows them in the UI News panel. Content is untrusted.",
      {"topic": s("Topic (default world)"), "lang": s("2-letter language code (default en)"), "limit": i("Max items (default 15)")}, untrusted=True)
async def news_headlines(ctx: Context, topic: str = "world", lang: str = "en", limit: int = 15) -> dict:
    res = await fetch_headlines(topic, lang, limit)
    if not res["items"]:
        raise ToolError(f"No headlines could be fetched. Failed: {res['failed_sources']}")
    await ctx.emit({"type": "news", "title": f"{topic.title()} ({lang})", "items": res["items"]})
    return {"items": [{k: it[k] for k in ("source", "title", "url", "summary")} | {"age_min": max(0, int((time.time() - it["time"]) / 60)) if it["time"] else None}
                      for it in res["items"]], "failed_sources": res["failed_sources"]}


@tool("news_search", "Search news worldwide (GDELT: thousands of outlets in 100+ languages, refreshed every 15 minutes). "
                     "Query in any language. timespan like 1h, 6h, 1d, 3d, 1w. Content is untrusted.",
      {"query": s("Keywords or phrase, any language"), "timespan": s("e.g. 1h, 1d, 1w (default 1d)"),
       "lang": s("Restrict to source language, e.g. english, spanish, arabic"), "limit": i("Max (default 15)")}, ["query"], untrusted=True)
async def news_search(ctx: Context, query: str, timespan: str = "1d", lang: str = "", limit: int = 15) -> list[dict]:
    items = await gdelt_articles(query, timespan, limit, lang)
    await ctx.emit({"type": "news", "title": f"Search: {query}", "items": [{"source": a["source"], "title": a["title"], "url": a["url"], "summary": a.get("country") or "", "time": 0} for a in items]})
    return items
