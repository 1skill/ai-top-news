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
from concurrent.futures import ThreadPoolExecutor, as_completed

import feedparser
import requests
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "sources.yaml"
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
# Rendering
# --------------------------------------------------------------------------- #
def _star_label(stars: int) -> str:
    if stars >= 1000:
        return f"{stars / 1000:.1f}k".replace(".0k", "k")
    return str(stars)


def render_news_cards(news: list[dict]) -> str:
    if not news:
        return '<p class="empty">暂时没有拉取到新闻，稍后自动重试。</p>'
    cards = []
    for n in news:
        teaser = html.escape(n["teaser"]) if n["teaser"] else ""
        cards.append(
            f"""<a class="card news-card" href="{html.escape(n['link'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="badge">{html.escape(n['source'])}</span><span class="ago">{html.escape(n['ago'])}</span></div>
  <h3 class="card-title">{html.escape(n['title'])}</h3>
  {f'<p class="card-teaser">{teaser}</p>' if teaser else ''}
</a>"""
        )
    return "\n".join(cards)


def render_repo_cards(repos: list[dict]) -> str:
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
        desc = html.escape(r["description"]) if r["description"] else "—"
        cards.append(
            f"""<a class="card repo-card" href="{html.escape(r['url'])}" target="_blank" rel="noopener">
  <div class="card-meta"><span class="repo-name">{html.escape(r['name'])}</span>{new_badge}</div>
  <p class="card-teaser">{desc}</p>
  <div class="repo-foot">
    <span class="stars">★ {_star_label(r['stars'])}</span>
    {lang}
    <span class="ago">建于 {html.escape(r['created_ago'])}</span>
  </div>
  <div class="topics">{topics}</div>
</a>"""
        )
    return "\n".join(cards)


def render_html(cfg: dict, news: list[dict], repos: list[dict]) -> str:
    site = cfg.get("site", {})
    title = site.get("title", "AI Top News")
    subtitle = site.get("subtitle", "")
    updated = NOW.strftime("%Y-%m-%d %H:%M UTC")

    page = HTML_SHELL
    page = page.replace("{{TITLE}}", html.escape(title))
    page = page.replace("{{SUBTITLE}}", html.escape(subtitle))
    page = page.replace("{{UPDATED}}", html.escape(updated))
    page = page.replace("{{NEWS_COUNT}}", str(len(news)))
    page = page.replace("{{REPO_COUNT}}", str(len(repos)))
    page = page.replace("{{NEWS_CARDS}}", render_news_cards(news))
    page = page.replace("{{REPO_CARDS}}", render_repo_cards(repos))
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
  section { margin-top: 34px; }
  .sec-head { display: flex; align-items: baseline; gap: 10px; margin: 0 0 14px;
    border-bottom: 1px solid var(--border); padding-bottom: 8px; }
  .sec-head h2 { font-size: 1.25rem; margin: 0; }
  .sec-head .count { color: var(--muted); font-size: .82rem; }
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
        <span><span class="dot"></span>最后更新 {{UPDATED}}</span>
        <span>· {{NEWS_COUNT}} 条新闻</span>
        <span>· {{REPO_COUNT}} 个项目</span>
        <span>· 每日自动刷新</span>
      </div>
    </header>

    <section id="news">
      <div class="sec-head"><h2>📰 AI 每日新闻</h2><span class="count">{{NEWS_COUNT}} 条 · 按时间排序</span></div>
      <div class="grid">
        {{NEWS_CARDS}}
      </div>
    </section>

    <section id="radar">
      <div class="sec-head"><h2>🛰️ GitHub AI 项目雷达</h2><span class="count">{{REPO_COUNT}} 个新兴项目 · 按星标排序</span></div>
      <div class="grid">
        {{REPO_CARDS}}
      </div>
    </section>

    <footer>
      <p>{{TITLE}} · 数据来自各 RSS 源与 GitHub Search API · 由 GitHub Actions 每日自动构建</p>
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
    log("== collecting github radar ==")
    repos = collect_repos(cfg)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "index.html").write_text(render_html(cfg, news, repos), encoding="utf-8")
    (OUT_DIR / "data.json").write_text(
        json.dumps(
            {
                "generated_at": NOW.isoformat(),
                "news": news,
                "repos": repos,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    # A .nojekyll file keeps GitHub Pages from touching our output.
    (OUT_DIR / ".nojekyll").write_text("", encoding="utf-8")

    log(f"== done: {len(news)} news, {len(repos)} repos -> {OUT_DIR} ==")
    if not news and not repos:
        log("[error] no data collected at all")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
