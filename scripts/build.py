#!/usr/bin/env python3
"""Build the "AI Top News" static site.

Pulls two kinds of data and renders a single self-contained page:

  1. AI 每日新闻      - latest headlines from a list of RSS/Atom feeds.
  2. GitHub AI 项目雷达 - newly created, fast-rising AI repos from GitHub search.

Output is written to ./public/ (index.html + data.json), ready for GitHub Pages.

Every network call is defensive: a failing feed or query is logged and skipped
so a single bad source never breaks the build.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import os
import pathlib
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
SITES_PATH = ROOT / "config" / "sites.yaml"
OUT_DIR = ROOT / "public"

USER_AGENT = "ai-top-news-bot/1.0 (+https://github.com/1skill/ai-top-news)"
NOW = dt.datetime.now(dt.timezone.utc)

GITHUB_API = "https://api.github.com/search/repositories"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def human_delta(when: dt.datetime | None) -> str:
    """Return a compact 'x hours ago' style label."""
    if when is None:
        return ""
    delta = NOW - when
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 3600:
        mins = secs // 60
        return f"{mins} 分钟前" if mins else "刚刚"
    if secs < 86400:
        return f"{secs // 3600} 小时前"
    days = secs // 86400
    if days < 30:
        return f"{days} 天前"
    return f"{days // 30} 个月前"


# --------------------------------------------------------------------------- #
# News feeds
# --------------------------------------------------------------------------- #
def _parse_published(entry) -> dt.datetime | None:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            try:
                return dt.datetime(*val[:6], tzinfo=dt.timezone.utc)
            except (TypeError, ValueError):
                continue
    return None


def fetch_feed(source: dict) -> list[dict]:
    name = source.get("name", source["url"])
    limit = int(source.get("limit", 8))
    try:
        resp = requests.get(
            source["url"], headers={"User-Agent": USER_AGENT}, timeout=25
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - one bad feed must not fail the build
        log(f"[warn] feed failed: {name}: {exc}")
        return []

    parsed = feedparser.parse(resp.content)
    items: list[dict] = []
    for entry in parsed.entries[:limit]:
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title or not link:
            continue
        published = _parse_published(entry)
        summary = (entry.get("summary") or "").strip()
        # feedparser summaries may contain HTML; strip tags crudely for a teaser.
        teaser = _strip_html(summary)[:220]
        items.append(
            {
                "title": title,
                "link": link,
                "source": name,
                "published_iso": published.isoformat() if published else None,
                "published_ts": published.timestamp() if published else 0.0,
                "ago": human_delta(published),
                "teaser": teaser,
            }
        )
    log(f"[ok]   feed: {name} -> {len(items)} items")
    return items


def _strip_html(text: str) -> str:
    out, depth = [], 0
    for ch in text:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        elif depth == 0:
            out.append(ch)
    return html.unescape("".join(out)).strip()


def collect_news(cfg: dict) -> list[dict]:
    feeds = cfg.get("news_feeds", [])
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_feed, f) for f in feeds]
        for fut in as_completed(futures):
            results.extend(fut.result())

    # De-duplicate by link, keep the newest ordering.
    seen: set[str] = set()
    unique: list[dict] = []
    for item in sorted(results, key=lambda x: x["published_ts"], reverse=True):
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        unique.append(item)

    max_news = int(cfg.get("site", {}).get("max_news", 36))
    return unique[:max_news]


# --------------------------------------------------------------------------- #
# GitHub AI radar
# --------------------------------------------------------------------------- #
def github_search(topic: str, since: str, token: str | None) -> list[dict]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    params = {
        "q": f"topic:{topic} created:>={since}",
        "sort": "stars",
        "order": "desc",
        "per_page": 25,
    }
    try:
        resp = requests.get(GITHUB_API, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] github topic failed: {topic}: {exc}")
        return []
    items = resp.json().get("items", [])
    log(f"[ok]   github topic: {topic} -> {len(items)} repos")
    return items


def collect_repos(cfg: dict) -> list[dict]:
    gh = cfg.get("github", {})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    window_days = int(gh.get("window_days", 45))
    new_within = int(gh.get("new_within_days", 10))
    min_stars = int(gh.get("min_stars", 40))
    since = (NOW - dt.timedelta(days=window_days)).date().isoformat()

    raw: list[dict] = []
    # Sequential to stay friendly with GitHub search rate limits.
    for topic in gh.get("topics", []):
        raw.extend(github_search(topic, since, token))

    by_id: dict[int, dict] = {}
    for repo in raw:
        rid = repo.get("id")
        if rid is None or rid in by_id:
            continue
        stars = repo.get("stargazers_count", 0)
        if stars < min_stars:
            continue
        created = repo.get("created_at")
        created_dt = None
        if created:
            try:
                created_dt = dt.datetime.fromisoformat(
                    created.replace("Z", "+00:00")
                )
            except ValueError:
                created_dt = None
        is_new = bool(
            created_dt and (NOW - created_dt).days <= new_within
        )
        by_id[rid] = {
            "name": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "description": (repo.get("description") or "").strip(),
            "stars": stars,
            "language": repo.get("language") or "",
            "topics": (repo.get("topics") or [])[:5],
            "created_iso": created_dt.isoformat() if created_dt else None,
            "created_ago": human_delta(created_dt),
            "is_new": is_new,
        }

    repos = sorted(by_id.values(), key=lambda r: r["stars"], reverse=True)
    max_repos = int(cfg.get("site", {}).get("max_repos", 30))
    return repos[:max_repos]


# --------------------------------------------------------------------------- #
# arXiv papers (research frontier)
# --------------------------------------------------------------------------- #
# The official API returns the newest submissions on demand — unlike the RSS
# feed, which is empty on days with no announcement (e.g. weekends).
ARXIV_API = "http://export.arxiv.org/api/query"


def _clean_arxiv_title(title: str) -> str:
    # Older arXiv titles append " (arXiv:xxxx [cs.AI])"; strip that tail.
    idx = title.find(" (arXiv:")
    title = title[:idx] if idx != -1 else title
    return " ".join(title.split())  # collapse the newlines arXiv inserts


def fetch_arxiv(cat: str, per_cat: int) -> list[dict]:
    try:
        resp = requests.get(
            ARXIV_API,
            params={
                "search_query": f"cat:{cat}",
                "sortBy": "submittedDate",
                "sortOrder": "descending",
                "max_results": per_cat,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] arxiv failed: {cat}: {exc}")
        return []
    parsed = feedparser.parse(resp.content)
    items = []
    for e in parsed.entries:
        title = _clean_arxiv_title((e.get("title") or "").strip())
        link = (e.get("link") or e.get("id") or "").strip()
        if not title or not link:
            continue
        published = _parse_published(e)
        summary = " ".join(_strip_html(e.get("summary") or "").split())
        items.append(
            {
                "title": title,
                "link": link,
                "source": f"arXiv · {cat}",
                "published_ts": published.timestamp() if published else 0.0,
                "ago": human_delta(published),
                "teaser": summary[:200],
            }
        )
    log(f"[ok]   arxiv: {cat} -> {len(items)} papers")
    return items


def collect_papers(cfg: dict) -> list[dict]:
    acfg = cfg.get("arxiv", {}) or {}
    if not acfg.get("enabled", True):
        return []
    cats = acfg.get("categories", ["cs.AI", "cs.CL", "cs.LG"])
    max_items = int(acfg.get("max_items", 24))
    per_cat = max(8, max_items // max(len(cats), 1) + 4)
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for r in pool.map(lambda c: fetch_arxiv(c, per_cat), cats):
            results.extend(r)
    seen: set[str] = set()
    unique: list[dict] = []
    for item in sorted(results, key=lambda x: x["published_ts"], reverse=True):
        if item["link"] in seen:
            continue
        seen.add(item["link"])
        unique.append(item)
    return unique[:max_items]


# --------------------------------------------------------------------------- #
# Hugging Face trending models (what people are actually running)
# --------------------------------------------------------------------------- #
HF_MODELS = "https://huggingface.co/api/models"


def collect_models(cfg: dict) -> list[dict]:
    mcfg = cfg.get("huggingface", {}) or {}
    if not mcfg.get("enabled", True):
        return []
    limit = int(mcfg.get("max_items", 24))
    try:
        resp = requests.get(
            HF_MODELS,
            params={"sort": "trendingScore", "direction": "-1", "limit": limit},
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] huggingface failed: {exc}")
        return []
    out = []
    for m in resp.json():
        mid = m.get("id") or m.get("modelId")
        if not mid:
            continue
        out.append(
            {
                "name": mid,
                "url": f"https://huggingface.co/{mid}",
                "likes": m.get("likes", 0),
                "downloads": m.get("downloads", 0),
                "task": m.get("pipeline_tag") or "",
            }
        )
    log(f"[ok]   huggingface -> {len(out)} models")
    return out[:limit]


# --------------------------------------------------------------------------- #
# Hacker News AI discussions (community & product pulse)
# --------------------------------------------------------------------------- #
HN_SEARCH = "https://hn.algolia.com/api/v1/search"


def fetch_hn(query: str, min_points: int) -> list[dict]:
    try:
        resp = requests.get(
            HN_SEARCH,
            params={
                "query": query,
                "tags": "story",
                "numericFilters": f"points>={min_points}",
                "hitsPerPage": 30,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=25,
        )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] hn failed: {query}: {exc}")
        return []
    items = []
    for h in resp.json().get("hits", []):
        title = (h.get("title") or "").strip()
        oid = h.get("objectID")
        if not title or not oid:
            continue
        ts = h.get("created_at_i")
        when = dt.datetime.fromtimestamp(ts, dt.timezone.utc) if ts else None
        items.append(
            {
                "title": title,
                "link": f"https://news.ycombinator.com/item?id={oid}",
                "article": h.get("url") or "",
                "source": "Hacker News",
                "points": h.get("points", 0),
                "comments": h.get("num_comments", 0),
                "ago": human_delta(when),
            }
        )
    log(f"[ok]   hn: {query!r} -> {len(items)} stories")
    return items


def collect_hackernews(cfg: dict) -> list[dict]:
    hcfg = cfg.get("hacker_news", {}) or {}
    if not hcfg.get("enabled", True):
        return []
    queries = hcfg.get("queries", ["AI", "LLM"])
    min_points = int(hcfg.get("min_points", 50))
    by_id: dict[str, dict] = {}
    for q in queries:
        for it in fetch_hn(q, min_points):
            by_id[it["link"]] = it  # de-duplicate by HN story id
    items = sorted(by_id.values(), key=lambda x: x["points"], reverse=True)
    return items[: int(hcfg.get("max_items", 20))]


# --------------------------------------------------------------------------- #
# Curated design / inspiration sites (a growing, hand-picked bookmark wall)
# --------------------------------------------------------------------------- #
def collect_sites() -> list[dict]:
    """Load the hand-curated sites from config/sites.yaml (if present)."""
    if not SITES_PATH.exists():
        return []
    try:
        with open(SITES_PATH, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except Exception as exc:  # noqa: BLE001
        log(f"[warn] sites.yaml failed: {exc}")
        return []
    sites = []
    for s in data.get("sites", []):
        url = (s.get("url") or "").strip()
        name = (s.get("name") or "").strip()
        if not url or not name:
            continue
        sites.append(
            {
                "name": name,
                "url": url,
                "category": (s.get("category") or "未分类").strip(),
                "desc": (s.get("desc") or "").strip(),
                "host": url.split("//")[-1].strip("/"),
                "thumb": (s.get("thumb") or "").strip(),
            }
        )
    log(f"[ok]   sites: {len(sites)} curated sites")
    return sites


# --------------------------------------------------------------------------- #
# Translation (make headlines & descriptions Chinese)
# --------------------------------------------------------------------------- #
TRANSLATE_ENDPOINT = "https://translate.googleapis.com/translate_a/single"


def _is_cjk(text: str) -> bool:
    """True if the text is already mostly Chinese (skip translating it)."""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    letters = sum(1 for ch in text if ch.isalpha())
    return letters > 0 and cjk / max(letters, 1) > 0.3


def translate_one(text: str, target: str) -> str:
    text = (text or "").strip()
    if not text or _is_cjk(text):
        return text
    try:
        resp = requests.get(
            TRANSLATE_ENDPOINT,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": target,
                "dt": "t",
                "q": text,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        parts = [seg[0] for seg in data[0] if seg and seg[0]]
        out = "".join(parts).strip()
        return out or text
    except Exception as exc:  # noqa: BLE001 - fall back to the original text
        log(f"[warn] translate failed ({text[:30]!r}): {exc}")
        return text


def _fill_original(items: list[dict]) -> None:
    for it in items:
        it["title_zh"] = it.get("title", "")
        it["teaser_zh"] = it.get("teaser", "")


def localize(
    cfg: dict,
    news: list[dict],
    repos: list[dict],
    papers: list[dict] | None = None,
    hn: list[dict] | None = None,
) -> None:
    """Translate English titles/teasers/descriptions to Chinese, in place.

    Each unique string is translated once; anything that fails keeps its
    original text so the build never breaks.
    """
    papers = papers or []
    hn = hn or []
    tcfg = cfg.get("translate", {}) or {}
    if not tcfg.get("enabled", True):
        for n in news:
            n["title_zh"], n["teaser_zh"] = n["title"], n["teaser"]
        _fill_original(papers)
        for h in hn:
            h["title_zh"] = h["title"]
        for r in repos:
            r["description_zh"] = r["description"]
        return

    target = tcfg.get("target", "zh-CN")
    texts: set[str] = set()
    for group in (news, papers):
        for it in group:
            texts.add(it["title"])
            if it.get("teaser"):
                texts.add(it["teaser"])
    for h in hn:
        texts.add(h["title"])
    for r in repos:
        if r["description"]:
            texts.add(r["description"])

    mapping: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(translate_one, t, target): t for t in texts}
        for fut in as_completed(futures):
            mapping[futures[fut]] = fut.result()

    for group in (news, papers):
        for it in group:
            it["title_zh"] = mapping.get(it["title"], it["title"])
            it["teaser_zh"] = (
                mapping.get(it["teaser"], it["teaser"]) if it.get("teaser") else ""
            )
    for h in hn:
        h["title_zh"] = mapping.get(h["title"], h["title"])
    for r in repos:
        r["description_zh"] = (
            mapping.get(r["description"], r["description"]) if r["description"] else ""
        )

    changed = sum(1 for k, v in mapping.items() if v != k)
    log(f"[ok]   translated {changed}/{len(mapping)} strings -> {target}")


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _star_label(stars: int) -> str:
    if stars >= 1000:
        return f"{stars / 1000:.1f}k".replace(".0k", "k")
    return str(stars)


def render_news_cards(news: list[dict], show_original: bool = True) -> str:
    if not news:
        return '<p class="empty">暂时没有拉取到新闻，稍后自动重试。</p>'
    cards = []
    for n in news:
        title_zh = n.get("title_zh") or n["title"]
        orig = ""
        if show_original and n["title"] and n["title"] != title_zh:
            orig = f'<p class="card-orig">{html.escape(n["title"])}</p>'
        teaser_zh = n.get("teaser_zh") or ""
        teaser = (
            f'<p class="card-teaser">{html.escape(teaser_zh)}</p>' if teaser_zh else ""
        )
        cards.append(
            f"""<a class="card news-card" href="{html.escape(n['link'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="badge">{html.escape(n['source'])}</span><span class="ago">{html.escape(n['ago'])}</span></div>
  <h3 class="card-title">{html.escape(title_zh)}</h3>
  {orig}
  {teaser}
</a>"""
        )
    return "\n".join(cards)


def render_repo_cards(repos: list[dict], show_original: bool = True) -> str:
    if not repos:
        return '<p class="empty">暂时没有拉取到项目，稍后自动重试。</p>'
    cards = []
    for r in repos:
        topics = "".join(
            f'<span class="topic">{html.escape(t)}</span>' for t in r["topics"]
        )
        new_badge = '<span class="new-badge">🆕 NEW</span>' if r["is_new"] else ""
        lang = (
            f'<span class="lang">{html.escape(r["language"])}</span>'
            if r["language"]
            else ""
        )
        desc_zh = r.get("description_zh") or r["description"]
        desc = html.escape(desc_zh) if desc_zh else "—"
        orig = ""
        if show_original and r["description"] and r["description"] != desc_zh:
            orig = f'<p class="card-orig">{html.escape(r["description"])}</p>'
        cards.append(
            f"""<a class="card repo-card" href="{html.escape(r['url'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="repo-name">{html.escape(r['name'])}</span>{new_badge}</div>
  <p class="card-teaser">{desc}</p>
  {orig}
  <div class="repo-foot">
    <span class="stars">★ {_star_label(r['stars'])}</span>
    {lang}
    <span class="ago">建于 {html.escape(r['created_ago'])}</span>
  </div>
  <div class="topics">{topics}</div>
</a>"""
        )
    return "\n".join(cards)


def render_paper_cards(papers: list[dict], show_original: bool = True) -> str:
    if not papers:
        return '<p class="empty">暂时没有拉取到论文，稍后自动重试。</p>'
    cards = []
    for p in papers:
        title_zh = p.get("title_zh") or p["title"]
        orig = ""
        if show_original and p["title"] and p["title"] != title_zh:
            orig = f'<p class="card-orig">{html.escape(p["title"])}</p>'
        teaser_zh = p.get("teaser_zh") or ""
        teaser = (
            f'<p class="card-teaser">{html.escape(teaser_zh)}</p>' if teaser_zh else ""
        )
        cards.append(
            f"""<a class="card news-card" href="{html.escape(p['link'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="badge">{html.escape(p['source'])}</span><span class="ago">{html.escape(p['ago'])}</span></div>
  <h3 class="card-title">{html.escape(title_zh)}</h3>
  {orig}
  {teaser}
</a>"""
        )
    return "\n".join(cards)


def render_model_cards(models: list[dict]) -> str:
    if not models:
        return '<p class="empty">暂时没有拉取到模型，稍后自动重试。</p>'
    cards = []
    for m in models:
        task = (
            f'<span class="topic">{html.escape(m["task"])}</span>' if m["task"] else ""
        )
        cards.append(
            f"""<a class="card repo-card" href="{html.escape(m['url'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="repo-name">{html.escape(m['name'])}</span></div>
  <div class="repo-foot">
    <span class="stars">❤ {_star_label(m['likes'])}</span>
    <span class="lang">↓ {_star_label(m['downloads'])}</span>
  </div>
  <div class="topics">{task}</div>
</a>"""
        )
    return "\n".join(cards)


def render_hn_cards(items: list[dict], show_original: bool = True) -> str:
    if not items:
        return '<p class="empty">暂时没有拉取到讨论，稍后自动重试。</p>'
    cards = []
    for h in items:
        title_zh = h.get("title_zh") or h["title"]
        orig = ""
        if show_original and h["title"] and h["title"] != title_zh:
            orig = f'<p class="card-orig">{html.escape(h["title"])}</p>'
        cards.append(
            f"""<a class="card news-card" href="{html.escape(h['link'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="badge">{html.escape(h['source'])}</span><span class="ago">{html.escape(h['ago'])}</span></div>
  <h3 class="card-title">{html.escape(title_zh)}</h3>
  {orig}
  <div class="repo-foot">
    <span class="stars">▲ {h['points']}</span>
    <span class="ago">💬 {h['comments']} 讨论</span>
  </div>
</a>"""
        )
    return "\n".join(cards)


def _mshot(url: str, width: int = 640) -> str:
    # WordPress mShots: free, keyless, on-demand screenshots.
    quoted = urllib.parse.quote(url, safe="")
    return f"https://s.wordpress.com/mshots/v1/{quoted}?w={width}"


def _thumb_img(site: dict, name_attr: str) -> str:
    """Build a screenshot <img> with a three-step fallback chain.

    thum.io renders reliably and fast; if it fails we fall back to mShots,
    and finally to the site's favicon so a card is never blank. A site may
    also pin its own image via a `thumb:` field in sites.yaml.

    All URLs are plain (no quotes / no '&'), so they inline safely into the
    double-quoted onerror attribute below.
    """
    if site.get("thumb"):
        return f'<img loading="lazy" src="{html.escape(site["thumb"])}" alt="{name_attr}">'
    url = site["url"]
    thumio = f"https://image.thum.io/get/width/640/crop/450/{url}"
    mshots = _mshot(url)
    favicon = f"https://icons.duckduckgo.com/ip3/{site['host']}.ico"
    # First error -> try mShots; if that also errors -> favicon (as a small logo).
    onerror = (
        "this.onerror=function(){this.onerror=null;"
        "this.classList.add('thumb-fallback');"
        f"this.src='{favicon}'}};this.src='{mshots}'"
    )
    return (
        f'<img loading="lazy" src="{thumio}" '
        f'onerror="{onerror}" alt="{name_attr}">'
    )


def render_sites_panel(sites: list[dict]) -> str:
    if not sites:
        return '<p class="empty">还没有收藏网站，发给我就会自动整理到这里。</p>'

    # Group by category, preserving first-seen order.
    groups: dict[str, list[dict]] = {}
    for s in sites:
        groups.setdefault(s["category"], []).append(s)

    blocks = []
    for cat, items in groups.items():
        thumbs, rows = [], []
        for s in items:
            name = html.escape(s["name"])
            url = html.escape(s["url"])
            desc = html.escape(s["desc"]) if s["desc"] else ""
            host = html.escape(s["host"])
            thumbs.append(
                f"""<a class="site-card" href="{url}" target="_blank" rel="noopener">
  <div class="thumb">{_thumb_img(s, name)}</div>
  <div class="site-info"><span class="site-name">{name}</span>
    {f'<p class="site-desc">{desc}</p>' if desc else ''}
    <span class="site-host">{host}</span>
  </div>
</a>"""
            )
            rows.append(
                f"""<a class="site-row" href="{url}" target="_blank" rel="noopener">
  <span class="site-name">{name}</span>
  {f'<span class="site-desc">{desc}</span>' if desc else ''}
  <span class="site-host">{host}</span>
</a>"""
            )
        blocks.append(
            f"""<div class="sites-cat-block">
  <h3 class="sites-cat">{html.escape(cat)} <span class="count">{len(items)}</span></h3>
  <div class="sites-grid">
    {"".join(thumbs)}
  </div>
  <div class="sites-list">
    {"".join(rows)}
  </div>
</div>"""
        )

    toolbar = """<div class="sites-toolbar">
      <button class="view-btn active" data-view="grid">🖼 缩略图</button>
      <button class="view-btn" data-view="list">☰ 列表</button>
    </div>"""
    script = """<script>
      (function () {
        var body = document.getElementById('sites-body');
        var btns = document.querySelectorAll('.view-btn');
        btns.forEach(function (b) {
          b.addEventListener('click', function () {
            btns.forEach(function (x) { x.classList.toggle('active', x === b); });
            body.classList.remove('mode-grid', 'mode-list');
            body.classList.add('mode-' + b.dataset.view);
          });
        });
      })();
    </script>"""
    return (
        toolbar
        + '<div id="sites-body" class="mode-grid">'
        + "\n".join(blocks)
        + "</div>"
        + script
    )


def render_html(
    cfg: dict,
    news: list[dict],
    repos: list[dict],
    papers: list[dict] | None = None,
    models: list[dict] | None = None,
    hn: list[dict] | None = None,
    sites: list[dict] | None = None,
) -> str:
    papers = papers or []
    models = models or []
    hn = hn or []
    site = cfg.get("site", {})
    title = site.get("title", "AI Top News")
    subtitle = site.get("subtitle", "")
    updated = NOW.strftime("%Y-%m-%d %H:%M UTC")
    show_original = bool((cfg.get("translate", {}) or {}).get("show_original", True))

    # Ordered sections. `always` ones show even when empty; the rest are
    # hidden entirely if a source returned nothing that day.
    sections = [
        {
            "id": "news", "icon": "📰", "label": "每日新闻", "count": len(news),
            "sub": f"{len(news)} 条 · 按时间排序",
            "cards": render_news_cards(news, show_original), "always": True,
        },
        {
            "id": "papers", "icon": "📄", "label": "arXiv 论文", "count": len(papers),
            "sub": f"{len(papers)} 篇 · 最新提交",
            "cards": render_paper_cards(papers, show_original),
        },
        {
            "id": "models", "icon": "🤗", "label": "热门模型", "count": len(models),
            "sub": f"{len(models)} 个 · Hugging Face 趋势",
            "cards": render_model_cards(models),
        },
        {
            "id": "hn", "icon": "💬", "label": "HN 热议", "count": len(hn),
            "sub": f"{len(hn)} 条 · 按热度排序",
            "cards": render_hn_cards(hn, show_original),
        },
        {
            "id": "radar", "icon": "🛰️", "label": "项目雷达", "count": len(repos),
            "sub": f"{len(repos)} 个新兴项目 · 按星标排序",
            "cards": render_repo_cards(repos, show_original), "always": True,
        },
        {
            "id": "sites", "icon": "🎨", "label": "灵感站", "count": len(sites),
            "sub": f"{len(sites)} 个收藏 · 缩略图 / 列表可切换",
            "cards": render_sites_panel(sites), "raw": True,
        },
    ]
    kept = [s for s in sections if s["count"] > 0 or s.get("always")]

    tabs, panels = [], []
    for i, s in enumerate(kept):
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab{active}" data-target="{s["id"]}" role="tab">'
            f'{s["icon"]} {s["label"]}<span class="tcount">{s["count"]}</span></button>'
        )
        body = (
            s["cards"]
            if s.get("raw")
            else f'<div class="grid">\n        {s["cards"]}\n      </div>'
        )
        panels.append(
            f'''<section id="{s['id']}" class="panel{active}" role="tabpanel">
      <div class="sec-head"><h2>{s['icon']} {s['label']}</h2><span class="count">{s['sub']}</span></div>
      {body}
    </section>'''
        )

    meta = (
        f'<span><span class="dot"></span>最后更新 {html.escape(updated)}</span>'
        + "".join(f'<span>· {s["count"]} {s["label"]}</span>' for s in kept)
        + '<span>· 每天早晚各更新一次</span>'
    )

    page = HTML_SHELL
    page = page.replace("{{TITLE}}", html.escape(title))
    page = page.replace("{{SUBTITLE}}", html.escape(subtitle))
    page = page.replace("{{META}}", meta)
    page = page.replace("{{TABS}}", "\n      ".join(tabs))
    page = page.replace("{{PANELS}}", "\n    ".join(panels))
    page = page.replace("{{YEAR}}", str(NOW.year))
    return page


HTML_SHELL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  :root {
    --bg: #f6f7f9; --panel: #ffffff; --text: #14161a; --muted: #5b6472;
    --border: #e6e8ec; --accent: #4f46e5; --accent-soft: #eef0fe;
    --star: #b8860b; --new: #16a34a; --shadow: 0 1px 3px rgba(20,22,26,.06);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --bg: #0d0f13; --panel: #161a21; --text: #e8eaed; --muted: #9aa4b2;
      --border: #262c36; --accent: #8b93ff; --accent-soft: #1c2030;
      --star: #e3b341; --new: #4ade80; --shadow: 0 1px 3px rgba(0,0,0,.3);
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
      "Hiragino Sans GB", "Microsoft YaHei", Roboto, Helvetica, Arial, sans-serif;
    line-height: 1.5; -webkit-font-smoothing: antialiased;
  }
  .wrap { max-width: 1040px; margin: 0 auto; padding: 24px 18px 64px; }
  header.hero { padding: 20px 0 8px; }
  h1 { font-size: 1.9rem; margin: 0 0 4px; letter-spacing: -.02em; }
  .subtitle { color: var(--muted); margin: 0 0 14px; font-size: 1rem; }
  .meta-row { display: flex; flex-wrap: wrap; gap: 8px 16px; color: var(--muted);
    font-size: .82rem; align-items: center; }
  .dot { width: 6px; height: 6px; border-radius: 50%; background: var(--new);
    display: inline-block; margin-right: 6px; vertical-align: middle; }
  /* Tab switcher */
  .tabs { position: sticky; top: 0; z-index: 10; display: flex; gap: 8px;
    background: var(--bg); padding: 12px 0 10px; margin-top: 14px;
    border-bottom: 1px solid var(--border); }
  .tab { font: inherit; cursor: pointer; border: 1px solid var(--border);
    background: var(--panel); color: var(--muted); padding: 8px 16px;
    border-radius: 999px; font-weight: 600; font-size: .92rem;
    transition: background .12s ease, color .12s ease, border-color .12s ease; }
  .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .tab .tcount { opacity: .85; font-size: .76rem; margin-left: 4px; }
  .panel { display: none; }
  .panel.active { display: block; animation: fade .18s ease; }
  @keyframes fade { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; } }
  section { margin-top: 20px; }
  .sec-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 14px;
    border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  .sec-head h2 { font-size: 1.25rem; margin: 0; }
  .sec-head .count { color: var(--muted); font-size: .82rem; }
  .card-orig { color: var(--muted); font-size: .76rem; margin: 4px 0 0;
    font-style: italic; }
  .grid { display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); }
  .card {
    display: block; background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; padding: 14px 16px; text-decoration: none;
    color: inherit; box-shadow: var(--shadow); transition: transform .12s ease,
      border-color .12s ease;
  }
  .card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .card-meta { display: flex; align-items: center; justify-content: space-between;
    gap: 8px; margin-bottom: 8px; }
  .badge { background: var(--accent-soft); color: var(--accent);
    font-size: .72rem; font-weight: 600; padding: 3px 8px; border-radius: 999px;
    white-space: nowrap; }
  .ago { color: var(--muted); font-size: .74rem; white-space: nowrap; }
  .card-title { font-size: 1rem; margin: 0; line-height: 1.35; }
  .card-teaser { color: var(--muted); font-size: .86rem; margin: 8px 0 0; }
  .repo-name { font-weight: 700; font-size: .95rem; color: var(--accent);
    word-break: break-all; }
  .new-badge { color: var(--new); font-size: .7rem; font-weight: 700;
    border: 1px solid var(--new); border-radius: 999px; padding: 2px 7px;
    white-space: nowrap; }
  .repo-foot { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 10px;
    font-size: .78rem; color: var(--muted); align-items: center; }
  .stars { color: var(--star); font-weight: 700; }
  .lang { color: var(--text); }
  .topics { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px; }
  .topic { background: var(--accent-soft); color: var(--accent); font-size: .68rem;
    padding: 2px 7px; border-radius: 6px; }
  .empty { color: var(--muted); }
  /* Curated sites: thumbnail / list toggle */
  .sites-toolbar { display: flex; gap: 8px; margin-bottom: 16px; }
  .view-btn { font: inherit; cursor: pointer; border: 1px solid var(--border);
    background: var(--panel); color: var(--muted); padding: 6px 14px;
    border-radius: 999px; font-size: .84rem; font-weight: 600; }
  .view-btn.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .sites-cat-block { margin-bottom: 26px; }
  .sites-cat { font-size: 1rem; margin: 0 0 12px; display: flex; align-items: center;
    gap: 8px; }
  .sites-cat .count { color: var(--muted); font-size: .78rem; font-weight: 400; }
  /* grid (thumbnail) view */
  .sites-grid { display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fill, minmax(240px, 1fr)); }
  .site-card { display: block; background: var(--panel); border: 1px solid var(--border);
    border-radius: 14px; overflow: hidden; text-decoration: none; color: inherit;
    box-shadow: var(--shadow); transition: transform .12s ease, border-color .12s ease; }
  .site-card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .thumb { aspect-ratio: 16 / 10; background: var(--accent-soft); overflow: hidden;
    display: flex; align-items: center; justify-content: center; }
  .thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .thumb img.thumb-fallback { width: 56px; height: 56px; object-fit: contain;
    opacity: .8; }
  .site-info { padding: 11px 13px 13px; }
  .site-name { font-weight: 700; font-size: .95rem; }
  .site-desc { color: var(--muted); font-size: .82rem; margin: 5px 0 0; }
  .site-host { color: var(--accent); font-size: .74rem; margin-top: 7px; display: block;
    word-break: break-all; }
  /* list view */
  .sites-list { display: none; flex-direction: column; gap: 8px; }
  .site-row { display: flex; flex-wrap: wrap; align-items: baseline; gap: 4px 12px;
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 14px; text-decoration: none; color: inherit; box-shadow: var(--shadow); }
  .site-row:hover { border-color: var(--accent); }
  .site-row .site-name { flex: 0 0 auto; }
  .site-row .site-desc { flex: 1 1 200px; margin: 0; }
  .site-row .site-host { margin: 0; flex: 0 0 auto; }
  #sites-body.mode-list .sites-grid { display: none; }
  #sites-body.mode-list .sites-list { display: flex; }
  footer { margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--border);
    color: var(--muted); font-size: .78rem; text-align: center; }
  footer a { color: var(--accent); text-decoration: none; }
</style>
</head>
<body>
  <div class="wrap">
    <header class="hero">
      <h1>{{TITLE}}</h1>
      <p class="subtitle">{{SUBTITLE}}</p>
      <div class="meta-row">
        {{META}}
      </div>
    </header>

    <nav class="tabs" role="tablist">
      {{TABS}}
    </nav>

    {{PANELS}}

    <script>
      (function () {
        var tabs = document.querySelectorAll('.tab');
        var panels = document.querySelectorAll('.panel');
        function activate(target) {
          var known = false;
          tabs.forEach(function (t) {
            var on = t.dataset.target === target;
            t.classList.toggle('active', on);
            if (on) known = true;
          });
          if (!known) return;
          panels.forEach(function (p) { p.classList.toggle('active', p.id === target); });
          if (history.replaceState) history.replaceState(null, '', '#' + target);
        }
        tabs.forEach(function (t) {
          t.addEventListener('click', function () { activate(t.dataset.target); });
        });
        // Honor a #section hash on load so links can deep-link a tab.
        var hash = (location.hash || '').replace('#', '');
        if (hash) activate(hash);
      })();
    </script>

    <footer>
      <p>{{TITLE}} · 数据来自 RSS 新闻源、arXiv、Hugging Face、Hacker News 与 GitHub · 由 GitHub Actions 每天自动构建</p>
      <p>© {{YEAR}} · <a href="https://github.com/1skill/ai-top-news">源代码</a></p>
    </footer>
  </div>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> int:
    cfg = load_config()
    log("== collecting news ==")
    news = collect_news(cfg)
    log("== collecting arxiv papers ==")
    papers = collect_papers(cfg)
    log("== collecting huggingface models ==")
    models = collect_models(cfg)
    log("== collecting hacker news ==")
    hn = collect_hackernews(cfg)
    log("== collecting github radar ==")
    repos = collect_repos(cfg)
    log("== loading curated sites ==")
    sites = collect_sites()

    log("== translating to Chinese ==")
    localize(cfg, news, repos, papers, hn)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(
        render_html(cfg, news, repos, papers, models, hn, sites), encoding="utf-8"
    )
    (OUT_DIR / "data.json").write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "news": news,
                "papers": papers,
                "models": models,
                "hacker_news": hn,
                "repos": repos,
                "sites": sites,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # A .nojekyll file keeps GitHub Pages from touching our output.
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    total = len(news) + len(papers) + len(models) + len(hn) + len(repos)
    log(
        f"== done: {len(news)} news, {len(papers)} papers, {len(models)} models, "
        f"{len(hn)} hn, {len(repos)} repos -> {OUT_DIR} =="
    )
    if total == 0:
        log("[error] no data collected at all")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
