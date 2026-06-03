#!/usr/bin/env python3
"""
Morning Brief — pull market-moving news from Alpaca's news feed,
rank it, and push the top items to phone via ntfy.sh.

Designed to fire ~8 AM ET on weekdays before US market open.
"""

from __future__ import annotations

import html
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
SITE_DIR = ROOT / "docs"  # GitHub Pages serves from /docs on main

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
NTFY_URL = "https://ntfy.sh"
GTRANS_URL = "https://translate.googleapis.com/translate_a/single"

ARTICLE_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}
LEAD_TARGET_CHARS = 420       # market news lead length
LEAD_MAX_CHARS = 520
GENERAL_LEAD_TARGET = 850     # general/world news — longer for English practice
GENERAL_LEAD_MAX = 1000
NTFY_SUMMARY_MAX = 450        # per-article cap when packing ntfy push (HTML uses full text)
CHUNK_SIZE = 3                # articles per ntfy push (fits the 4096-byte free-tier limit)

# Only show market news that touches one of these tickers.
WATCH_SYMBOLS = {"SMH", "QQQ", "GLD", "MU", "VOO", "MRVL"}

BOILERPLATE_PATTERNS = [
    r"subscribe", r"newsletter", r"read (also|more|next)",
    r"don'?t miss", r"click here", r"photo (by|courtesy)", r"image (by|via|courtesy)",
    r"©", r"all rights reserved", r"follow us on", r"sign up for",
    r"benzinga'?s", r"market news and data",
]
BOILERPLATE_RE = re.compile("|".join(BOILERPLATE_PATTERNS), re.IGNORECASE)

# Window: from yesterday 4pm ET (16:00 -04) ~= 20:00 UTC, to now.
# Run at 8am ET = 12:00 UTC, so default lookback of 16h covers the overnight news cycle.
LOOKBACK_HOURS = 16
MAX_ARTICLES = 6
FETCH_LIMIT = 50

# Tickers whose news moves the broad market.
INDEX_TICKERS = {"SPY", "QQQ", "DIA", "IWM", "VIX", "VOO", "VTI"}
MEGA_CAPS = {
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "BRK.B", "AVGO", "JPM", "LLY", "V", "XOM", "UNH", "WMT",
}
TOP_SOURCES = {"Reuters", "Bloomberg", "CNBC", "WSJ", "Wall Street Journal",
               "Financial Times", "Barron's", "Dow Jones Newswires",
               "NYT", "New York Times", "BBC", "NPR"}

# General-news RSS feeds (no API key required).
GENERAL_FEEDS: list[tuple[str, str]] = [
    ("NYT", "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml"),
    ("BBC", "https://feeds.bbci.co.uk/news/world/rss.xml"),
    ("NPR", "https://feeds.npr.org/1001/rss.xml"),
]
RSS_ITEMS_PER_FEED = 8

MACRO_KEYWORDS = [
    r"\bfed\b", r"\bfomc\b", r"\brate cut\b", r"\brate hike\b",
    r"\bcpi\b", r"\bppi\b", r"\binflation\b", r"\bpayroll", r"\bjobs report\b",
    r"\bunemployment\b", r"\brecession\b", r"\btariff", r"\btrade war\b",
    r"\bsanction", r"\boil price", r"\bcrude\b", r"\bgeopolit",
    r"\bwar\b", r"\bstrike\b", r"\bshutdown\b", r"\bdefault\b",
    r"\bdebt ceiling\b", r"\btreasury yield", r"\byield curve\b",
]
EARNINGS_KEYWORDS = [
    r"\bearnings\b", r"\bbeats?\b", r"\bmisses?\b", r"\bguidance\b",
    r"\bupgrade", r"\bdowngrade", r"\bprice target\b", r"\bacquir",
    r"\bmerger\b", r"\bbuyback\b", r"\bdividend\b",
]


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def fetch_rss(source: str, url: str) -> list[dict]:
    """Pull recent items from an RSS feed and normalize to Alpaca's article shape."""
    try:
        r = requests.get(url, headers=ARTICLE_FETCH_HEADERS, timeout=10)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  RSS fetch failed for {source}: {e}", file=sys.stderr)
        return []

    items = root.findall(".//item")[:RSS_ITEMS_PER_FEED]
    out: list[dict] = []
    for it in items:
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        desc = (it.findtext("description") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            dt = parsedate_to_datetime(pub) if pub else None
            if dt and dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            created_at = dt.isoformat() if dt else None
        except Exception:
            created_at = None
        if not title or not link:
            continue
        out.append({
            "headline": title,
            "summary": re.sub(r"<[^>]+>", "", desc).strip(),
            "url": link,
            "source": source,
            "symbols": [],
            "created_at": created_at,
        })
    return out


def fetch_news(api_key: str, api_secret: str, start: datetime, end: datetime) -> list[dict]:
    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }
    params = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort": "desc",
        "limit": FETCH_LIMIT,
        "include_content": "false",
        "exclude_contentless": "true",
    }
    r = requests.get(ALPACA_NEWS_URL, headers=headers, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("news", [])


def score_article(article: dict, now: datetime) -> float:
    score = 0.0
    headline = (article.get("headline") or "").lower()
    summary = (article.get("summary") or "").lower()
    text = f"{headline} {summary}"
    source = (article.get("source") or "").strip()
    symbols = set(article.get("symbols") or [])

    if any(s.lower() in source.lower() for s in TOP_SOURCES):
        score += 3
    elif source:
        score += 1

    score += 8 * len(symbols & WATCH_SYMBOLS)
    score += 5 * len(symbols & INDEX_TICKERS)
    score += 3 * len(symbols & MEGA_CAPS)

    extra_syms = symbols - INDEX_TICKERS - MEGA_CAPS
    if len(extra_syms) >= 5:
        score += 2
    elif len(extra_syms) >= 2:
        score += 1

    for pat in MACRO_KEYWORDS:
        if re.search(pat, text):
            score += 4
            break
    for pat in EARNINGS_KEYWORDS:
        if re.search(pat, text):
            score += 2
            break

    created = parse_ts(article.get("created_at"))
    if created:
        age_h = (now - created).total_seconds() / 3600
        if age_h <= 6:
            score += 2
        elif age_h <= 12:
            score += 1

    return score


def parse_ts(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def dedupe(articles: list[dict]) -> list[dict]:
    """Drop near-duplicates: same first 6 words of headline."""
    seen: set[str] = set()
    out: list[dict] = []
    for a in articles:
        key = " ".join((a.get("headline") or "").lower().split()[:6])
        if key in seen:
            continue
        seen.add(key)
        out.append(a)
    return out


def extract_lead(url: str, target_chars: int = LEAD_TARGET_CHARS,
                 max_chars: int = LEAD_MAX_CHARS) -> str:
    """Fetch article and return its lead paragraphs, skipping boilerplate.
    Returns '' on failure."""
    if not url:
        return ""
    try:
        r = requests.get(url, headers=ARTICLE_FETCH_HEADERS, timeout=12)
        r.raise_for_status()
    except Exception as e:
        print(f"  body fetch failed for {url}: {e}", file=sys.stderr)
        return ""

    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "header", "form"]):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup

    kept: list[str] = []
    total = 0
    for p in container.find_all("p"):
        text = p.get_text(" ", strip=True)
        if len(text) < 50:
            continue
        if BOILERPLATE_RE.search(text):
            continue
        kept.append(text)
        total += len(text) + 1
        if total >= target_chars:
            break

    lead = " ".join(kept).strip()
    if len(lead) > max_chars:
        cut = lead[:max_chars].rsplit(". ", 1)
        lead = (cut[0] + ".") if len(cut) == 2 else (lead[:max_chars - 3] + "...")
    return lead


def translate_zh(text: str) -> str:
    """English -> Simplified Chinese via Google's public translate endpoint.
    Returns empty string on failure so the caller can skip the line silently."""
    if not text.strip():
        return ""
    try:
        r = requests.get(
            GTRANS_URL,
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()
        return "".join(seg[0] for seg in data[0] if seg and seg[0]).strip()
    except Exception as e:
        print(f"  translate failed: {e}", file=sys.stderr)
        return ""


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Morning Brief">
<title>Morning Brief — {date_short}</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="icon.svg">
<link rel="apple-touch-icon" href="icon.svg">
<style>
  :root {{
    --bg: #ffffff; --bg-card: #f8fafc; --fg: #0f172a; --fg-muted: #64748b;
    --accent: #15803d; --border: #e2e8f0;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  html, body {{ background: var(--bg); color: var(--fg); }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
    line-height: 1.55; max-width: 760px; margin: 0 auto;
    padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }}
  header {{ padding: 28px 20px 16px; border-bottom: 1px solid var(--border); }}
  h1 {{ font-size: 28px; font-weight: 700; letter-spacing: -0.02em; }}
  header .date {{ color: var(--fg-muted); font-size: 14px; margin-top: 4px; }}
  section {{ padding: 0 20px; }}
  h2 {{
    font-size: 12px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.1em; color: var(--accent); margin: 28px 0 12px;
  }}
  article {{
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 14px; padding: 18px 20px; margin-bottom: 14px;
  }}
  article .meta {{
    font-size: 12px; color: var(--fg-muted); margin-bottom: 8px;
    display: flex; gap: 8px; flex-wrap: wrap;
  }}
  .symbols {{ color: var(--accent); font-weight: 600; }}
  article h3 {{ font-size: 17px; font-weight: 600; line-height: 1.35; margin-bottom: 6px; }}
  article h3 a {{ color: var(--fg); text-decoration: none; }}
  article h3 a:hover {{ color: var(--accent); }}
  .zh-line {{ color: var(--fg-muted); font-size: 15px; margin-bottom: 12px; }}
  .summary {{ font-size: 15px; margin: 10px 0 6px; }}
  .zh-summary {{ font-size: 14px; color: var(--fg-muted); }}
  footer {{
    padding: 24px 20px 32px; color: var(--fg-muted); font-size: 12px;
    text-align: center; border-top: 1px solid var(--border); margin-top: 28px;
  }}
  footer a {{ color: var(--accent); text-decoration: none; }}
</style>
</head>
<body>
<header>
  <h1>Morning Brief</h1>
  <div class="date">{date_long}</div>
</header>
{sections}
<footer>
  Generated {gen_ts} UTC · Updates daily at 9 AM ET
</footer>
</body>
</html>
"""

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="42" fill="#0a0a0a"/>
  <circle cx="96" cy="72" r="22" fill="#4ade80"/>
  <rect x="36" y="118" width="120" height="4" rx="2" fill="#4ade80"/>
  <rect x="36" y="134" width="92"  height="3" rx="1.5" fill="#888"/>
  <rect x="36" y="148" width="108" height="3" rx="1.5" fill="#888"/>
  <rect x="36" y="162" width="76"  height="3" rx="1.5" fill="#888"/>
</svg>
"""

MANIFEST_JSON = """{
  "name": "Morning Brief",
  "short_name": "Brief",
  "description": "Daily market and world news digest",
  "start_url": ".",
  "scope": ".",
  "display": "standalone",
  "background_color": "#ffffff",
  "theme_color": "#ffffff",
  "icons": [
    { "src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any" }
  ]
}
"""


def _html_escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _article_html(article: dict, index: int) -> str:
    headline = html.unescape(article.get("headline") or "").strip()
    source = (article.get("source") or "").strip()
    syms = article.get("symbols") or []
    url = article.get("url") or "#"
    summary = article.get("_summary") or ""
    zh_head = article.get("_zh_headline") or ""
    zh_sum = article.get("_zh_summary") or ""

    sym_html = f'<span class="symbols">[{",".join(syms[:5])}]</span>' if syms else ""
    zh_head_html = f'<div class="zh-line">{_html_escape(zh_head)}</div>' if zh_head else ""
    sum_html = f'<p class="summary">{_html_escape(summary)}</p>' if summary else ""
    zh_sum_html = f'<p class="zh-summary">{_html_escape(zh_sum)}</p>' if zh_sum else ""

    return (
        f'<article>\n'
        f'  <div class="meta">{sym_html}<span>{_html_escape(source)}</span></div>\n'
        f'  <h3><a href="{_html_escape(url)}" target="_blank" rel="noopener">'
        f'{index}. {_html_escape(headline)}</a></h3>\n'
        f'  {zh_head_html}\n'
        f'  {sum_html}\n'
        f'  {zh_sum_html}\n'
        f'</article>'
    )


def render_html(articles: list[dict], output_dir: Path) -> Path:
    """Write index.html, manifest, and icon to output_dir. Returns index.html path.

    Articles must already carry _summary / _zh_headline / _zh_summary fields
    (populated during format_message). Splits articles into World vs Markets.
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    world = [a for a in articles if not (a.get("symbols") or [])]
    markets = [a for a in articles if a.get("symbols")]

    sections_html = []
    idx = 1
    if world:
        items = "\n".join(_article_html(a, idx + i) for i, a in enumerate(world))
        sections_html.append(f"<section><h2>World</h2>\n{items}\n</section>")
        idx += len(world)
    if markets:
        items = "\n".join(_article_html(a, idx + i) for i, a in enumerate(markets))
        sections_html.append(f"<section><h2>Markets</h2>\n{items}\n</section>")
    if not articles:
        sections_html.append(
            "<section><p style='padding:20px;color:#888;'>"
            "No notable news in the overnight window. Check back later.</p></section>"
        )

    now_local = datetime.now()
    page = HTML_TEMPLATE.format(
        date_short=now_local.strftime("%-m/%d"),
        date_long=now_local.strftime("%A, %B %-d, %Y"),
        gen_ts=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
        sections="\n".join(sections_html),
    )

    index_path = output_dir / "index.html"
    index_path.write_text(page, encoding="utf-8")
    (output_dir / "manifest.webmanifest").write_text(MANIFEST_JSON, encoding="utf-8")
    (output_dir / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    return index_path


def enrich_articles(articles: list[dict]) -> None:
    """Fetch article bodies + translate headlines & summaries, stashing results on each dict.
    Done once so the ntfy push and HTML render reuse the same translations / scrapes.

    General news (no symbols) gets a longer lead than market news, for more English to read.
    """
    for i, a in enumerate(articles, 1):
        headline = html.unescape(a.get("headline") or "").strip()
        fallback = html.unescape(a.get("summary") or "").strip()
        is_general = not (a.get("symbols") or [])
        print(f"  [{i}/{len(articles)}] fetching ({'general' if is_general else 'market'}): {headline[:55]}...")
        if is_general:
            summary = extract_lead(a.get("url") or "",
                                    target_chars=GENERAL_LEAD_TARGET,
                                    max_chars=GENERAL_LEAD_MAX) or fallback
        else:
            summary = extract_lead(a.get("url") or "") or fallback
        a["_summary"] = summary
        a["_zh_headline"] = translate_zh(headline)
        a["_zh_summary"] = translate_zh(summary) if summary else ""


def format_message(articles: list[dict], part: int = 1, total: int = 1,
                   start_index: int = 1) -> tuple[str, str]:
    """Return (title, body) for one ntfy push.

    Articles must already be enriched (see enrich_articles).
    `part`/`total` annotate the title when the brief spans multiple notifications.
    `start_index` is the number of the first article in this chunk.
    """
    today = datetime.now().strftime("%a %-m/%d")
    suffix = f"  ({part}/{total})" if total > 1 else ""
    title = f"Morning Brief — {today}{suffix}"
    blocks: list[str] = []
    for offset, a in enumerate(articles):
        i = start_index + offset
        headline = html.unescape(a.get("headline") or "").strip()
        source = (a.get("source") or "").strip()
        syms = a.get("symbols") or []
        sym_str = f"[{','.join(syms[:3])}] " if syms else ""
        suffix_str = f" — {source}" if source else ""

        parts = [f"{i}. {sym_str}{headline}{suffix_str}"]
        if a.get("_zh_headline"):
            parts.append(f"   {a['_zh_headline']}")
        # Truncate per-article so 3 long articles still fit in one 4096-byte ntfy push.
        summary = a.get("_summary") or ""
        if len(summary) > NTFY_SUMMARY_MAX:
            summary = summary[:NTFY_SUMMARY_MAX - 3].rsplit(" ", 1)[0] + "..."
        zh_sum = a.get("_zh_summary") or ""
        if len(zh_sum) > NTFY_SUMMARY_MAX:
            zh_sum = zh_sum[:NTFY_SUMMARY_MAX - 3] + "..."
        if summary:
            parts.append(f"   • {summary}")
            if zh_sum:
                parts.append(f"   • {zh_sum}")
        blocks.append("\n".join(parts))
    body = "\n\n".join(blocks) if blocks else "No notable market-moving news in the overnight window."
    return title, body


def push_ntfy(topic: str, title: str, body: str, click_url: str | None) -> None:
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": "chart_with_upwards_trend",
    }
    if click_url:
        headers["Click"] = click_url
    r = requests.post(
        f"{NTFY_URL}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()


def main() -> int:
    env = load_env(ENV_PATH)
    api_key = env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
    api_secret = env.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET")
    topic = env.get("NTFY_NEWS_TOPIC") or os.environ.get("NTFY_NEWS_TOPIC")

    missing = [n for n, v in [("ALPACA_API_KEY", api_key),
                              ("ALPACA_API_SECRET", api_secret),
                              ("NTFY_NEWS_TOPIC", topic)] if not v]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=LOOKBACK_HOURS)
    print(f"Fetching news from {start.isoformat()} to {now.isoformat()}")

    articles = fetch_news(api_key, api_secret, start, now)
    print(f"Fetched {len(articles)} market articles from Alpaca")

    for source, url in GENERAL_FEEDS:
        items = fetch_rss(source, url)
        in_window = [a for a in items
                     if (ts := parse_ts(a.get("created_at"))) and ts >= start]
        print(f"Fetched {len(items)} from {source} ({len(in_window)} in window)")
        articles.extend(in_window)

    # Filter: keep general news (no symbols) AND market news that touches the watchlist.
    before = len(articles)
    articles = [
        a for a in articles
        if not (a.get("symbols") or [])
        or (set(a.get("symbols") or []) & WATCH_SYMBOLS)
    ]
    print(f"Filtered {before} -> {len(articles)} articles (kept general + watchlist: {sorted(WATCH_SYMBOLS)})")

    scored = sorted(
        ((score_article(a, now), a) for a in articles),
        key=lambda p: p[0],
        reverse=True,
    )
    top = dedupe([a for s, a in scored if s > 0])[:MAX_ARTICLES]
    # World/general news first (no symbol tags), market news after. Stable sort preserves score order within each group.
    top.sort(key=lambda a: 0 if not a.get("symbols") else 1)
    print(f"Selected {len(top)} top articles")
    for i, a in enumerate(top, 1):
        print(f"  {i}. [{','.join(a.get('symbols', []) or [])}] {a.get('headline')}")

    enrich_articles(top)

    # Always render the HTML page (for GitHub Pages / PWA)
    index_path = render_html(top, SITE_DIR)
    print(f"Rendered site: {index_path}")

    # Push to ntfy in chunks
    if not top:
        title, body = format_message(top)
        push_ntfy(topic, title, body, None)
        print(f"Pushed empty brief to ntfy topic '{topic}'")
        return 0

    chunks = [top[i:i + CHUNK_SIZE] for i in range(0, len(top), CHUNK_SIZE)]
    total = len(chunks)
    for idx, chunk in enumerate(chunks, 1):
        start_index = (idx - 1) * CHUNK_SIZE + 1
        title, body = format_message(chunk, part=idx, total=total, start_index=start_index)
        click_url = chunk[0].get("url")
        push_ntfy(topic, title, body, click_url)
        print(f"Pushed part {idx}/{total} to ntfy topic '{topic}' ({len(body.encode('utf-8'))} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
