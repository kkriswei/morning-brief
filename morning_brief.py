#!/usr/bin/env python3
"""Evidence-first US market brief.

The brief first establishes what the broad US market actually did using delayed
consolidated daily bars.  It then ranks same-session reporting that can explain
that move.  News is never presented as a proven cause: source diversity,
timestamp alignment, and confidence are shown explicitly.

The normal scheduled morning run explains the latest completed session.  A
second after-close run can explain the current session once a completed bar is
available.  Both runs also include a small world-news section.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from statistics import median
from string import Template
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
ENV_PATH = ROOT / ".env"
SITE_DIR = ROOT / "docs"
NY_TZ = ZoneInfo("America/New_York")

ALPACA_NEWS_URL = "https://data.alpaca.markets/v1beta1/news"
ALPACA_BARS_URL = "https://data.alpaca.markets/v2/stocks/bars"
NTFY_URL = "https://ntfy.sh"
GTRANS_URL = "https://translate.googleapis.com/translate_a/single"

ARTICLE_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
}

NEWS_PAGE_SIZE = 50
MAX_NEWS_PAGES = 10
MAX_MARKET_DRIVERS = 4
MAX_WORLD_ARTICLES = 3
MAX_SECTOR_GROUPS = 4
MAX_SECTOR_ARTICLES = 2
MAX_SPECIAL_ARTICLES = 2
RSS_ITEMS_PER_FEED = 20
NEWS_LOOKBACK_HOURS = 48
MAX_NEWS_LOOKBACK_HOURS = 96
SECTION_NEWS_LOOKBACK_HOURS = 36
SUMMARY_TARGET_CHARS = 360
SUMMARY_MAX_CHARS = 480
COMPLETED_SESSION_DELAY_MINUTES = 30

BROAD_MARKET_SYMBOLS = {
    "SPY": "S&P 500",
    "QQQ": "Nasdaq 100",
    "DIA": "Dow",
    "IWM": "Russell 2000",
}
SECTOR_SYMBOLS = {
    "SMH": "Semiconductors",
    "XLK": "Technology",
    "XLC": "Communication Services",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLI": "Industrials",
    "XLV": "Health Care",
    "XLY": "Consumer Discretionary",
}
CROSS_ASSET_SYMBOLS = {
    "TLT": "Long Treasuries ETF",
    "USO": "Oil ETF",
    "UUP": "Dollar ETF",
}
SPECIAL_SYMBOLS = {
    "SPCX": "SPCX",
    "MRVL": "Marvell",
}
MARKET_SYMBOLS = {
    **BROAD_MARKET_SYMBOLS,
    **SECTOR_SYMBOLS,
    **CROSS_ASSET_SYMBOLS,
    **SPECIAL_SYMBOLS,
}

INDEX_TICKERS = set(BROAD_MARKET_SYMBOLS) | {"VIX", "VOO", "VTI"}
MEGA_CAPS = {
    "AAPL", "MSFT", "NVDA", "GOOGL", "GOOG", "AMZN", "META", "TSLA",
    "BRK.B", "AVGO", "JPM", "LLY", "V", "XOM", "UNH", "WMT",
}


@dataclass(frozen=True)
class FeedSpec:
    source: str
    url: str
    section: str
    authority: int


MARKET_FEEDS = [
    FeedSpec(
        "Bloomberg Markets",
        "https://feeds.bloomberg.com/markets/news.rss",
        "market",
        7,
    ),
    FeedSpec(
        "Financial Times Markets",
        "https://www.ft.com/markets?format=rss",
        "market",
        7,
    ),
    FeedSpec(
        "CNBC Markets",
        "https://www.cnbc.com/id/15839069/device/rss/rss.html",
        "market",
        6,
    ),
    FeedSpec(
        "MarketWatch",
        "https://feeds.marketwatch.com/marketwatch/topstories/",
        "market",
        5,
    ),
    FeedSpec(
        "NYT Business",
        "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml",
        "market",
        5,
    ),
    FeedSpec(
        "BBC Business",
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "market",
        4,
    ),
    FeedSpec(
        "Federal Reserve",
        "https://www.federalreserve.gov/feeds/press_all.xml",
        "market",
        7,
    ),
]

WORLD_FEEDS = [
    FeedSpec(
        "NYT",
        "https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml",
        "world",
        5,
    ),
    FeedSpec(
        "BBC",
        "https://feeds.bbci.co.uk/news/world/rss.xml",
        "world",
        5,
    ),
    FeedSpec(
        "NPR",
        "https://feeds.npr.org/1001/rss.xml",
        "world",
        4,
    ),
]


@dataclass(frozen=True)
class MarketMove:
    symbol: str
    label: str
    close: float
    previous_close: float
    change_pct: float
    session_date: date
    group: str


@dataclass
class MarketPulse:
    session_date: date | None
    moves: dict[str, MarketMove] = field(default_factory=dict)
    status: str = "unavailable"
    feed: str = "unavailable"
    note: str = "行情数据不足"

    @property
    def available(self) -> bool:
        return self.session_date is not None and "SPY" in self.moves

    @property
    def primary_change(self) -> float | None:
        move = self.moves.get("SPY")
        return move.change_pct if move else None


@dataclass
class DriverAssessment:
    articles: list[dict]
    confidence: str
    summary_zh: str
    categories: list[str]


@dataclass
class MarketOverview:
    label_zh: str
    tone: str
    summary_zh: str
    evidence_zh: list[str]


@dataclass
class SectorGroup:
    key: str
    label_zh: str
    proxy_symbol: str | None
    articles: list[dict]


@dataclass(frozen=True)
class SpecialWatchSpec:
    symbol: str
    label_zh: str
    keywords: tuple[str, ...]


CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "rates": (
        r"\bfed(?:eral reserve)?\b", r"\bfomc\b", r"interest rate",
        r"rate (?:cut|hike|decision|outlook)", r"treasury yield", r"bond yield",
    ),
    "inflation_jobs": (
        r"\bcpi\b", r"\bppi\b", r"inflation", r"payroll", r"jobs report",
        r"employment report", r"unemployment", r"job openings", r"\bjolts\b",
    ),
    "ai_chips": (
        r"artificial intelligence", r"\bai\b", r"semiconductor", r"\bchips?\b",
        r"nvidia", r"data cent(?:er|re)", r"hyperscaler",
    ),
    "earnings": (
        r"earnings", r"quarterly results", r"revenue", r"profit forecast",
        r"guidance", r"beats? estimates", r"misses? estimates",
    ),
    "oil_energy": (
        r"oil price", r"\bcrude\b", r"\bbrent\b", r"\bwti\b",
        r"opec", r"energy prices?", r"strait of hormuz",
    ),
    "geopolitics_trade": (
        r"tariff", r"trade war", r"sanction", r"middle east", r"iran",
        r"israel", r"ukraine", r"geopolit", r"ceasefire", r"strait of hormuz",
    ),
    "growth": (
        r"\bgdp\b", r"economic growth", r"recession", r"consumer spending",
        r"retail sales", r"manufacturing", r"services activity",
    ),
    "bonds_fx": (
        r"treasur", r"bond market", r"\bdollar\b", r"currency", r"yield curve",
    ),
}

CATEGORY_LABELS_ZH = {
    "rates": "利率与美联储预期",
    "inflation_jobs": "通胀与就业数据",
    "ai_chips": "AI 与半导体",
    "earnings": "公司业绩与指引",
    "oil_energy": "油价与能源",
    "geopolitics_trade": "地缘政治与贸易政策",
    "growth": "经济增长预期",
    "bonds_fx": "债券与美元",
}

SECTOR_PATTERNS: dict[str, tuple[str, ...]] = {
    "semiconductors": (
        r"semiconductors?", r"chipmakers?", r"\bchips?\b", r"\bnvidia\b",
        r"\bnvda\b", r"\bamd\b", r"\bmicron\b", r"\bmarvell\b",
        r"\bmrvl\b", r"\bbroadcom\b", r"\bavgo\b", r"\btsmc\b",
        r"\bintel\b", r"\bqualcomm\b", r"memory (?:chip|shortage)",
        r"\bfoundry\b", r"\bwafer\b",
    ),
    "technology_ai": (
        r"artificial intelligence", r"\bai\b", r"\bopenai\b", r"\bcloud\b",
        r"\bsoftware\b", r"\bmicrosoft\b", r"\bmsft\b", r"\balphabet\b",
        r"\bgoogle\b", r"\bmeta\b", r"\bamazon\b", r"\bamzn\b",
        r"cybersecurity", r"data cent(?:er|re)", r"large language model",
    ),
    "financials": (
        r"\bbanks?\b", r"\bfinancials?\b", r"\bjpmorgan\b", r"\bgoldman\b",
        r"morgan stanley", r"bank of america", r"\bcitigroup\b", r"\bcredit\b",
        r"\bfintech\b", r"\binsurance\b", r"private equity",
    ),
    "energy": (
        r"\boil\b", r"\bcrude\b", r"\bbrent\b", r"\bwti\b", r"\bopec\b",
        r"energy (?:sector|stocks?|prices?)", r"natural gas", r"\bexxon\b",
        r"\bchevron\b", r"strait of hormuz",
    ),
    "macro_rates": (
        r"\bfed(?:eral reserve)?\b", r"\bfomc\b", r"interest rates?", r"rate cut",
        r"rate hike", r"treasur", r"bond yields?", r"\binflation\b", r"\bcpi\b",
        r"\bppi\b", r"\bpayrolls?\b", r"jobs report", r"\bgdp\b",
        r"\brecession\b", r"\bdollar\b",
    ),
    "geopolitics_trade": (
        r"\biran\b", r"\bisrael\b", r"\bukraine\b", r"\brussia\b",
        r"\btariffs?\b", r"trade war", r"\bsanctions?\b", r"\bceasefire\b",
        r"strait of hormuz", r"geopolit",
    ),
    "consumer": (
        r"consumer", r"\bretail\b", r"\bwalmart\b", r"\bcostco\b",
        r"home depot", r"\bnike\b", r"\bstarbucks\b", r"restaurants?",
        r"\btravel\b", r"\bairlines?\b",
    ),
    "healthcare": (
        r"health ?care", r"\bpharma\b", r"\bdrugmakers?\b", r"\bbiotech\b",
        r"\bfda\b", r"\bmedicare\b", r"eli lilly", r"novo nordisk",
    ),
    "industrials": (
        r"\bindustrials?\b", r"\bdefen[cs]e\b", r"\baerospace\b",
        r"\bboeing\b", r"lockheed", r"caterpillar", r"\bmanufacturing\b",
        r"\bshipping\b", r"\btransport(?:ation)?\b",
    ),
}

SECTOR_LABELS_ZH = {
    "semiconductors": "半导体",
    "technology_ai": "科技与 AI",
    "financials": "金融",
    "energy": "能源与原油",
    "macro_rates": "宏观与利率",
    "geopolitics_trade": "地缘政治与贸易",
    "consumer": "消费",
    "healthcare": "医疗健康",
    "industrials": "工业与国防",
    "broad_market": "大盘综合",
}

SECTOR_PROXIES = {
    "semiconductors": "SMH",
    "technology_ai": "XLK",
    "financials": "XLF",
    "energy": "XLE",
    "consumer": "XLY",
    "healthcare": "XLV",
    "industrials": "XLI",
}

SECTOR_SYMBOL_HINTS = {
    "semiconductors": {"SMH", "SOXX", "NVDA", "AMD", "MU", "MRVL", "AVGO", "TSM", "INTC", "QCOM"},
    "technology_ai": {"XLK", "XLC", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "ORCL", "CRM"},
    "financials": {"XLF", "JPM", "BAC", "C", "GS", "MS", "WFC"},
    "energy": {"XLE", "USO", "XOM", "CVX", "COP", "OXY"},
    "consumer": {"XLY", "WMT", "COST", "HD", "NKE", "SBUX"},
    "healthcare": {"XLV", "LLY", "UNH", "JNJ", "PFE", "NVO"},
    "industrials": {"XLI", "BA", "CAT", "LMT", "RTX", "GE"},
}

SPECIAL_WATCH_SPECS = {
    "SPCX": SpecialWatchSpec(
        "SPCX",
        "SPCX / SpaceX 相关",
        ("spcx", "spacex", "starship", "starlink", "falcon 9", "falcon heavy"),
    ),
    "MRVL": SpecialWatchSpec(
        "MRVL",
        "MRVL · Marvell",
        ("mrvl", "marvell", "marvell technology"),
    ),
}

US_MARKET_RE = re.compile(
    r"(?:\bwall street\b|\bs&p(?: 500)?\b|\bnasdaq\b|\bdow(?: jones)?\b|"
    r"\brussell 2000\b|\bu\.s\. stocks?\b|\bus stocks?\b|"
    r"\bu\.s\. equities\b|\bus equities\b|\bu\.s\. futures\b|"
    r"\bmajor u\.s\. indexes\b|\bmajor us indexes\b|\bthe stock market\b)",
    re.IGNORECASE,
)
NON_US_MARKET_RE = re.compile(
    r"\b(?:japan(?:ese)?|korea(?:n)?|china|chinese|hong kong|india(?:n)?|"
    r"asia(?:n)?|europe(?:an)?|britain|british|german|french)\b",
    re.IGNORECASE,
)
MARKET_RECAP_RE = re.compile(
    r"stock market today|markets? wrap|wall street (?:ends|closes|rises|falls|"
    r"gains|slips|slides|rallies)|stocks (?:end|close|finish|rise|fall|gain|"
    r"drop|slip|slide|rally|rebound|sell off|sell-off)|"
    r"(?:s&p(?: 500)?|nasdaq|dow(?: jones)?).{0,45}(?:ends?|closes?|finishes?|"
    r"higher|lower|mixed|record)",
    re.IGNORECASE,
)
CAUSE_CONNECTOR_RE = re.compile(
    r"\b(?:as|after|amid|because|following|driven by|fueled by|weighed by|"
    r"boosted by|hurt by|pressured by)\b",
    re.IGNORECASE,
)
UP_RE = re.compile(
    r"\b(?:rise|rises|rose|rising|gain|gains|gained|higher|rally|rallies|"
    r"rallied|rebound|rebounds|surge|surges|jump|jumps|advance|advances)\b",
    re.IGNORECASE,
)
DOWN_RE = re.compile(
    r"\b(?:fall|falls|fell|falling|drop|drops|dropped|lower|slip|slips|slid|"
    r"slide|slides|slump|slumps|sink|sinks|sank|sinking|selloff|sell-off|"
    r"tumble|tumbles|decline|declines|loss|losses)\b",
    re.IGNORECASE,
)
MARKET_NEGATIVE_PHRASE_RE = re.compile(
    r"(?:\bwall street\b|\bu\.s\. stocks?\b|\bus stocks?\b|\bstocks?\b|"
    r"\bs&p(?: 500)?\b|\bnasdaq\b|\bdow(?: jones)?\b).{0,28}"
    r"(?:erase(?:s|d)? gains?|give(?:s|n)? up gains?|extend(?:s|ed)? losses?|"
    r"end(?:s|ed)? lower|close(?:s|d)? lower|fall(?:s|ing)?|fell|drop(?:s|ped)?|"
    r"slip(?:s|ped)?|slid|slide(?:s|d)?|slump(?:s|ed)?|sell(?:s|ing)? off|"
    r"sell-?off|tumble(?:s|d)?|decline(?:s|d)?)",
    re.IGNORECASE,
)
MARKET_POSITIVE_PHRASE_RE = re.compile(
    r"(?:\bwall street\b|\bu\.s\. stocks?\b|\bus stocks?\b|\bstocks?\b|"
    r"\bs&p(?: 500)?\b|\bnasdaq\b|\bdow(?: jones)?\b).{0,28}"
    r"(?:erase(?:s|d)? losses?|recover(?:s|ed)?|rebound(?:s|ed)?|"
    r"end(?:s|ed)? higher|close(?:s|d)? higher|rise(?:s|n)?|rose|"
    r"gain(?:s|ed)?|rall(?:y|ies|ied)|surge(?:s|d)?|jump(?:s|ed)?|"
    r"advance(?:s|d)?)",
    re.IGNORECASE,
)
LOW_VALUE_RE = re.compile(
    r"\b(?:opinion|prediction|price target|top \d+|\d+ stocks?\b|stocks? to buy|"
    r"what to watch|could be|may be|might be|old clip|technical analysis)\b",
    re.IGNORECASE,
)
PREMARKET_RE = re.compile(
    r"\b(?:futures?|pre[- ]?market|before the bell|ahead of (?:the )?open|"
    r"opening bell)\b",
    re.IGNORECASE,
)
CORE_NEWS_NOISE_RE = re.compile(
    r"\b(?:earnings scheduled|investors?' radars?|bulls and bears|"
    r"stocks? investors couldn'?t stop buzzing|this week in|weekly roundup|"
    r"why (?:is|are|did) .{0,45} stocks?|retirement|personal finance|"
    r"ideal .{0,30} strategy)\b|401\(k\)",
    re.IGNORECASE,
)
WORLD_IMPORTANCE_RE = re.compile(
    r"\b(?:war|ceasefire|election|president|prime minister|earthquake|hurricane|"
    r"central bank|sanction|tariff|nuclear|invasion|strike|peace talks)\b",
    re.IGNORECASE,
)

BOILERPLATE_RE = re.compile(
    r"subscribe|newsletter|read (?:also|more|next)|don'?t miss|click here|"
    r"photo (?:by|courtesy)|image (?:by|via|courtesy)|©|all rights reserved|"
    r"follow us on|sign up for|benzinga'?s|market news and data|"
    r"new video loaded|advertisement",
    re.IGNORECASE,
)


def load_env(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _auth_headers(api_key: str, api_secret: str) -> dict[str, str]:
    return {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": api_secret,
    }


def _element_text(element: ET.Element, *local_names: str) -> str:
    wanted = set(local_names)
    for child in element.iter():
        local = child.tag.rsplit("}", 1)[-1].lower()
        if local not in wanted:
            continue
        if local == "link" and child.attrib.get("href"):
            return child.attrib["href"].strip()
        if child.text and child.text.strip():
            return child.text.strip()
    return ""


def _clean_markup(value: str) -> str:
    if not value:
        return ""
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _trim_summary(value: str, maximum: int = SUMMARY_MAX_CHARS) -> str:
    clean = _clean_markup(value)
    if not clean:
        return ""
    sentences = re.split(r"(?<=[.!?])\s+", clean)
    kept: list[str] = []
    total = 0
    for sentence in sentences:
        if len(sentence) < 25 or BOILERPLATE_RE.search(sentence):
            continue
        kept.append(sentence)
        total += len(sentence) + 1
        if total >= SUMMARY_TARGET_CHARS:
            break
    summary = " ".join(kept).strip() or clean
    if len(summary) <= maximum:
        return summary
    cut = summary[:maximum].rsplit(". ", 1)
    return (cut[0] + ".") if len(cut) == 2 else summary[: maximum - 3].rstrip() + "..."


def fetch_rss(
    spec: FeedSpec,
    start: datetime,
    end: datetime,
    client=requests,
) -> list[dict]:
    """Fetch one RSS/Atom feed and normalize it to the article schema."""
    try:
        response = client.get(spec.url, headers=ARTICLE_FETCH_HEADERS, timeout=12)
        response.raise_for_status()
        root = ET.fromstring(response.content)
    except Exception as exc:
        print(f"  RSS fetch failed for {spec.source}: {exc}", file=sys.stderr)
        return []

    nodes = root.findall(".//item")
    if not nodes:
        nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "entry"]

    articles: list[dict] = []
    for node in nodes[:RSS_ITEMS_PER_FEED]:
        headline = _clean_markup(_element_text(node, "title"))
        url = _element_text(node, "link")
        summary = _clean_markup(_element_text(node, "description", "summary", "content", "encoded"))
        published = _element_text(node, "pubdate", "published", "updated", "date")
        created = parse_ts(published)
        if created and not (start <= created <= end):
            continue
        if not headline or not url:
            continue
        articles.append(
            {
                "headline": headline,
                "summary": summary,
                "content": summary,
                "url": url,
                "source": spec.source,
                "symbols": [],
                "created_at": created.isoformat() if created else None,
                "_section": spec.section,
                "_authority": spec.authority,
            }
        )
    return articles


def fetch_alpaca_news(
    api_key: str,
    api_secret: str,
    start: datetime,
    end: datetime,
    client=requests,
) -> list[dict]:
    """Fetch every available Alpaca news page, capped to bound runtime."""
    params: dict[str, str | int] = {
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sort": "desc",
        "limit": NEWS_PAGE_SIZE,
        "include_content": "true",
        "exclude_contentless": "true",
    }
    articles: list[dict] = []
    page_token: str | None = None

    for _ in range(MAX_NEWS_PAGES):
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token
        response = client.get(
            ALPACA_NEWS_URL,
            headers=_auth_headers(api_key, api_secret),
            params=page_params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        page = payload.get("news", [])
        for article in page:
            normalized = dict(article)
            normalized["source"] = (normalized.get("source") or "Benzinga").strip()
            normalized["symbols"] = normalized.get("symbols") or []
            normalized["_section"] = "market"
            normalized["_authority"] = 2
            articles.append(normalized)
        page_token = payload.get("next_page_token")
        if not page_token or not page:
            break
    return articles


def _fetch_daily_bar_pages(
    api_key: str,
    api_secret: str,
    start: datetime,
    end: datetime,
    feed: str,
    client=requests,
) -> dict[str, list[dict]]:
    params: dict[str, str | int] = {
        "symbols": ",".join(MARKET_SYMBOLS),
        "timeframe": "1Day",
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "adjustment": "all",
        "feed": feed,
        "sort": "asc",
        "limit": 10000,
    }
    bars: dict[str, list[dict]] = defaultdict(list)
    page_token: str | None = None
    for _ in range(4):
        page_params = dict(params)
        if page_token:
            page_params["page_token"] = page_token
        response = client.get(
            ALPACA_BARS_URL,
            headers=_auth_headers(api_key, api_secret),
            params=page_params,
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        for symbol, items in (payload.get("bars") or {}).items():
            bars[symbol].extend(items or [])
        page_token = payload.get("next_page_token")
        if not page_token:
            break
    return dict(bars)


def _latest_allowed_session_date(now: datetime) -> date:
    local = now.astimezone(NY_TZ)
    if local.time() >= time(16, COMPLETED_SESSION_DELAY_MINUTES):
        return local.date()
    return local.date() - timedelta(days=1)


def _bar_session_date(bar: dict) -> date | None:
    timestamp = parse_ts(bar.get("t"))
    return timestamp.astimezone(NY_TZ).date() if timestamp else None


def _classify_pulse(moves: dict[str, MarketMove]) -> str:
    spy = moves.get("SPY")
    if not spy:
        return "unavailable"
    broad = [
        moves[symbol].change_pct
        for symbol in BROAD_MARKET_SYMBOLS
        if symbol in moves
    ]
    if len(broad) >= 2 and max(broad) >= 0.35 and min(broad) <= -0.35:
        return "mixed"
    center = median(broad) if broad else spy.change_pct
    if spy.change_pct >= 1.0 and center > 0:
        return "strong_up"
    if spy.change_pct <= -1.0 and center < 0:
        return "strong_down"
    if spy.change_pct >= 0.25:
        return "up"
    if spy.change_pct <= -0.25:
        return "down"
    if broad and max(broad) - min(broad) >= 0.7:
        return "mixed"
    return "flat"


def build_market_pulse(
    raw_bars: dict[str, list[dict]],
    now: datetime,
    feed: str,
) -> MarketPulse:
    """Build a comparable, same-session pulse from raw daily bars."""
    allowed_date = _latest_allowed_session_date(now)
    dated: dict[str, list[tuple[date, dict]]] = {}

    for symbol, items in raw_bars.items():
        normalized: list[tuple[date, dict]] = []
        for bar in items:
            session_date = _bar_session_date(bar)
            if session_date and session_date <= allowed_date and bar.get("c") is not None:
                normalized.append((session_date, bar))
        normalized.sort(key=lambda pair: pair[0])
        dated[symbol] = normalized

    spy_rows = dated.get("SPY", [])
    if len(spy_rows) < 2:
        return MarketPulse(
            session_date=None,
            feed=feed,
            note="无法取得两个可比较的 SPY 已完成交易日，暂不判断涨跌原因。",
        )

    session_date = spy_rows[-1][0]
    moves: dict[str, MarketMove] = {}
    for symbol, rows in dated.items():
        current_index = next(
            (index for index, pair in enumerate(rows) if pair[0] == session_date),
            None,
        )
        if current_index is None or current_index == 0:
            continue
        current = rows[current_index][1]
        previous = rows[current_index - 1][1]
        close = float(current["c"])
        previous_close = float(previous["c"])
        if previous_close <= 0:
            continue
        if symbol in BROAD_MARKET_SYMBOLS:
            group = "broad"
        elif symbol in SECTOR_SYMBOLS:
            group = "sector"
        elif symbol in CROSS_ASSET_SYMBOLS:
            group = "cross"
        else:
            group = "special"
        moves[symbol] = MarketMove(
            symbol=symbol,
            label=MARKET_SYMBOLS.get(symbol, symbol),
            close=close,
            previous_close=previous_close,
            change_pct=(close / previous_close - 1) * 100,
            session_date=session_date,
            group=group,
        )

    if "SPY" not in moves:
        return MarketPulse(
            session_date=None,
            feed=feed,
            note="SPY 与其他指数不是同一交易日口径，暂不拼接数据。",
        )

    feed_note = (
        "延迟 SIP 全市场日线"
        if feed == "sip"
        else "IEX 单一交易所日线（SIP 不可用时的降级数据）"
    )
    return MarketPulse(
        session_date=session_date,
        moves=moves,
        status=_classify_pulse(moves),
        feed=feed,
        note=feed_note,
    )


def fetch_market_pulse(
    api_key: str,
    api_secret: str,
    now: datetime,
    client=requests,
) -> MarketPulse:
    end = now - timedelta(minutes=16)
    start = end - timedelta(days=16)
    errors: list[str] = []
    for feed in ("sip", "iex"):
        try:
            raw_bars = _fetch_daily_bar_pages(
                api_key, api_secret, start, end, feed, client=client
            )
            pulse = build_market_pulse(raw_bars, now, feed)
            if pulse.available:
                return pulse
            errors.append(f"{feed}: {pulse.note}")
        except Exception as exc:
            errors.append(f"{feed}: {exc}")
            print(f"  Market data fetch failed for {feed}: {exc}", file=sys.stderr)
    return MarketPulse(
        session_date=None,
        note="行情不可用，暂不判断涨跌原因。" + (" " + " | ".join(errors) if errors else ""),
    )


def _article_text(article: dict) -> str:
    return " ".join(
        _clean_markup(article.get(key) or "")
        for key in ("headline", "summary", "content")
    ).lower()


def article_categories(article: dict) -> set[str]:
    text = _article_text(article)
    return {
        category
        for category, patterns in CATEGORY_PATTERNS.items()
        if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns)
    }


def _article_direction(article: dict) -> int:
    text = _clean_markup(article.get("headline") or "")
    # Resolve the direction of the US equity subject before looking at generic
    # words.  This prevents "stocks slump, bonds gain" from becoming neutral
    # and treats "stocks erase gains" as negative rather than positive.
    if MARKET_NEGATIVE_PHRASE_RE.search(text):
        return -1
    if MARKET_POSITIVE_PHRASE_RE.search(text):
        return 1
    up = bool(UP_RE.search(text))
    down = bool(DOWN_RE.search(text))
    if up == down:
        return 0
    return 1 if up else -1


def _pulse_direction(pulse: MarketPulse) -> int:
    if pulse.status in {"strong_up", "up"}:
        return 1
    if pulse.status in {"strong_down", "down"}:
        return -1
    return 0


def _article_date_et(article: dict) -> date | None:
    created = parse_ts(article.get("created_at"))
    return created.astimezone(NY_TZ).date() if created else None


def _source_identity(article: dict) -> str:
    hostname = urlparse(article.get("url") or "").hostname
    return (hostname or article.get("source") or "unknown").lower().removeprefix("www.")


def score_market_article(article: dict, pulse: MarketPulse) -> float:
    if article.get("_section") != "market":
        return -100.0
    if pulse.session_date and _article_date_et(article) != pulse.session_date:
        # Never join a prior/next-day headline to a precise session move.
        return -100.0
    text = _article_text(article)
    headline = _clean_markup(article.get("headline") or "")
    score = float(article.get("_authority") or 1)
    broad = bool(US_MARKET_RE.search(text))
    headline_broad = bool(US_MARKET_RE.search(headline))
    # Recap language must be in the headline itself. Article bodies often carry
    # related-story widgets or generic market boilerplate that can otherwise
    # turn a company feature into a false market wrap.
    recap = broad and bool(MARKET_RECAP_RE.search(headline))
    created = parse_ts(article.get("created_at"))
    created_et = created.astimezone(NY_TZ) if created else None
    categories = article_categories(article)
    symbols = set(article.get("symbols") or [])
    source = (article.get("source") or "").lower()

    if NON_US_MARKET_RE.search(headline) and not US_MARKET_RE.search(headline):
        return -100.0
    if PREMARKET_RE.search(headline):
        # Futures and premarket pieces describe expectations before the session,
        # not evidence for why the completed close finished where it did.
        return -100.0
    if recap and created_et and created_et.time() < time(9, 30):
        # A pre-open "market wrap" on the session date is necessarily recapping
        # the previous trading day. Same calendar date alone is not sufficient.
        return -100.0
    constituent_shock = (
        pulse.status not in {"flat", "mixed", "unavailable"}
        and bool(symbols & MEGA_CAPS)
        and bool(categories & {"ai_chips", "earnings"})
        and _article_direction(article) == _pulse_direction(pulse)
    )
    if not broad and source != "federal reserve" and not constituent_shock:
        return -100.0
    causal_signal = (
        recap
        or bool(headline_broad and CAUSE_CONNECTOR_RE.search(headline))
        or _article_direction(article) != 0
    )
    if broad and source != "federal reserve" and not constituent_shock and not causal_signal:
        return -100.0
    if pulse.status in {"flat", "mixed"} and source != "federal reserve" and not recap:
        # A flat or split tape needs a market-wide wrap; an isolated company or
        # theme story is not enough to claim a broad-market driver.
        return -100.0

    article_direction = _article_direction(article)
    pulse_direction = _pulse_direction(pulse)
    if article_direction and pulse_direction and article_direction != pulse_direction:
        # An intraday story pointing the other way cannot explain the completed
        # session move, even if it came from a strong source on the same date.
        return -100.0

    if broad:
        score += 6
    if recap:
        score += 8
    if headline_broad and CAUSE_CONNECTOR_RE.search(headline):
        score += 3
    if broad and not recap and not (
        headline_broad and CAUSE_CONNECTOR_RE.search(headline)
    ) and not categories:
        # Market history/statistics describe the move but do not explain it.
        score -= 8
    score += min(len(categories) * 2, 5)

    if pulse.session_date:
        score += 6

    if article_direction and pulse_direction:
        score += 5
    elif article_direction and pulse.status in {"flat", "mixed"}:
        score -= 2

    if symbols & INDEX_TICKERS:
        score += 4
    score += min(2 * len(symbols & MEGA_CAPS), 4)

    if source == "federal reserve" and categories:
        score += 5
    if not broad and source != "federal reserve" and not (symbols & INDEX_TICKERS):
        score -= 4
    if LOW_VALUE_RE.search(text):
        score -= 8
    url = (article.get("url") or "").lower()
    if "/opinion/" in url or "unpaid external contributor" in text:
        score -= 8
    if symbols and not broad and not categories:
        score -= 5
    return score


def _headline_key(article: dict) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (article.get("headline") or "").lower())
    stop = {"the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "as"}
    return {word for word in words if word not in stop}


def _too_similar(left: dict, right: dict) -> bool:
    a = _headline_key(left)
    b = _headline_key(right)
    if not a or not b:
        return False
    return len(a & b) / len(a | b) >= 0.72


def select_market_drivers(
    articles: list[dict],
    pulse: MarketPulse,
    limit: int = MAX_MARKET_DRIVERS,
) -> list[dict]:
    scored = sorted(
        ((score_market_article(article, pulse), article) for article in articles),
        key=lambda pair: pair[0],
        reverse=True,
    )
    selected: list[dict] = []
    per_source: Counter[str] = Counter()
    for score, article in scored:
        if score < 12:
            continue
        source_id = _source_identity(article)
        if per_source[source_id] >= 2:
            continue
        if any(_too_similar(article, existing) for existing in selected):
            continue
        chosen = dict(article)
        chosen["_impact_score"] = round(score, 1)
        chosen["_categories"] = sorted(article_categories(article))
        chosen["_is_recap"] = bool(
            US_MARKET_RE.search(_article_text(article))
            and MARKET_RECAP_RE.search(_clean_markup(article.get("headline") or ""))
        )
        selected.append(chosen)
        per_source[source_id] += 1
        if len(selected) >= limit:
            break

    return selected


def _pulse_label_zh(status: str) -> str:
    return {
        "strong_up": "大盘显著上涨",
        "up": "大盘上涨",
        "strong_down": "大盘显著下跌",
        "down": "大盘下跌",
        "mixed": "主要指数明显分化",
        "flat": "主要指数整体变化不大",
        "unavailable": "行情数据不足",
    }.get(status, "行情数据不足")


def build_driver_assessment(
    pulse: MarketPulse,
    articles: list[dict],
) -> DriverAssessment:
    if not pulse.available:
        return DriverAssessment(
            articles=articles,
            confidence="不足",
            summary_zh="行情数据不足，暂不把任何新闻写成大盘涨跌原因。",
            categories=[],
        )
    if not articles:
        session = pulse.session_date.strftime("%Y-%m-%d") if pulse.session_date else "该交易日"
        return DriverAssessment(
            articles=[],
            confidence="不足",
            summary_zh=(
                f"{_pulse_label_zh(pulse.status)}，但没有找到与 {session} 同日、"
                "并且能和指数方向对应的可靠市场综述；暂不强行归因。"
            ),
            categories=[],
        )

    category_scores: defaultdict[str, float] = defaultdict(float)
    for article in articles:
        for category in article.get("_categories") or []:
            category_scores[category] += max(float(article.get("_impact_score") or 0), 1)
    categories = [
        category for category, _ in sorted(
            category_scores.items(), key=lambda item: item[1], reverse=True
        )[:2]
    ]
    sources = {_source_identity(article) for article in articles}
    has_recap = any(article.get("_is_recap") for article in articles)
    if len(sources) >= 2 and has_recap:
        confidence = "较高"
    elif len(sources) >= 2 or has_recap:
        confidence = "中等"
    else:
        confidence = "较低"

    category_text = "、".join(CATEGORY_LABELS_ZH.get(item, item) for item in categories)
    if pulse.status in {"flat", "mixed"}:
        detail = f"报道主要集中在{category_text}" if category_text else "报道没有形成单一共识"
        summary = (
            f"{_pulse_label_zh(pulse.status)}，没有单一主导催化剂。{detail}，"
            "更像多股力量互相抵消。"
        )
    else:
        detail = category_text or "下方同日市场综述"
        summary = (
            f"{_pulse_label_zh(pulse.status)}。同日市场报道最集中指向{detail}；"
            "这是当前证据最一致的解释，不代表唯一因果。"
        )
    return DriverAssessment(
        articles=articles,
        confidence=confidence,
        summary_zh=summary,
        categories=categories,
    )


def build_market_overview(
    pulse: MarketPulse,
    assessment: DriverAssessment,
) -> MarketOverview:
    """Turn verified market breadth into a concise bullish/bearish overview."""
    if not pulse.available:
        return MarketOverview(
            label_zh="数据不足",
            tone="unavailable",
            summary_zh="缺少可比较的完整交易日行情，暂不判断利好或利空。",
            evidence_zh=[pulse.note],
        )

    broad = [
        pulse.moves[symbol]
        for symbol in BROAD_MARKET_SYMBOLS
        if symbol in pulse.moves
    ]
    positive = [move for move in broad if move.change_pct > 0]
    negative = [move for move in broad if move.change_pct < 0]
    center = median([move.change_pct for move in broad]) if broad else 0.0

    if pulse.status in {"strong_up", "up"}:
        label = "偏利好"
        tone = "positive"
        summary = "主要指数方向向上，风险偏好占优；但仍需看板块是否同步扩散。"
    elif pulse.status in {"strong_down", "down"}:
        label = "偏利空"
        tone = "negative"
        summary = "主要指数方向向下，卖压占优；反弹新闻不足以改变收盘结论。"
    elif pulse.status == "mixed" and len(positive) >= 3 and center >= 0.15:
        label = "中性偏利好 · 结构分化"
        tone = "cautious-positive"
        summary = "上涨指数数量占优，但并非全面上涨，资金轮动比单一方向更重要。"
    elif pulse.status == "mixed" and len(negative) >= 3 and center <= -0.15:
        label = "中性偏利空 · 结构分化"
        tone = "cautious-negative"
        summary = "下跌指数数量占优，但并非全面下跌，弱势集中在部分板块。"
    elif pulse.status == "mixed":
        label = "中性 · 明显分化"
        tone = "neutral"
        summary = "指数互有涨跌，没有足够证据把今天概括成全面利好或全面利空。"
    else:
        label = "中性"
        tone = "neutral"
        summary = "大盘整体变化有限，新闻催化没有形成一致方向。"

    evidence = [
        f"四大指数中 {len(positive)} 个上涨、{len(negative)} 个下跌；中位变动 {center:+.2f}%。"
    ]
    sector_moves = sorted(
        (move for move in pulse.moves.values() if move.group == "sector"),
        key=lambda move: move.change_pct,
        reverse=True,
    )
    if sector_moves:
        evidence.append(
            f"领涨 {_sector_move_label_zh(sector_moves[0])} "
            f"{sector_moves[0].change_pct:+.2f}%；"
            f"落后 {_sector_move_label_zh(sector_moves[-1])} "
            f"{sector_moves[-1].change_pct:+.2f}%。"
        )
    if assessment.categories:
        category_text = "、".join(
            CATEGORY_LABELS_ZH.get(item, item) for item in assessment.categories
        )
        evidence.append(f"同日核心报道集中在：{category_text}。")
    elif assessment.confidence == "不足":
        evidence.append("没有达到门槛的同日市场综述，因此不强行归因。")

    return MarketOverview(label, tone, summary, evidence)


def _article_age_hours(article: dict, now: datetime) -> float | None:
    created = parse_ts(article.get("created_at"))
    if not created:
        return None
    return (now - created).total_seconds() / 3600


def _sector_scores(article: dict) -> dict[str, int]:
    headline = _clean_markup(article.get("headline") or "").lower()
    text = _article_text(article)
    symbols = {str(symbol).upper() for symbol in article.get("symbols") or []}
    scores: dict[str, int] = {}
    for key, patterns in SECTOR_PATTERNS.items():
        headline_hits = sum(
            1 for pattern in patterns if re.search(pattern, headline, re.IGNORECASE)
        )
        text_hits = sum(
            1 for pattern in patterns if re.search(pattern, text, re.IGNORECASE)
        )
        symbol_hit = bool(symbols & SECTOR_SYMBOL_HINTS.get(key, set()))
        score = headline_hits * 4 + min(text_hits, 3) + (5 if symbol_hit else 0)
        if score:
            scores[key] = score
    return scores


def primary_sector(article: dict) -> str | None:
    scores = _sector_scores(article)
    if scores:
        return max(scores, key=lambda key: scores[key])
    if US_MARKET_RE.search(_article_text(article)):
        return "broad_market"
    return None


def _sector_article_score(
    article: dict,
    now: datetime,
    driver_urls: set[str],
) -> float:
    if article.get("_section") != "market":
        return -100.0
    url = article.get("url") or ""
    is_driver = url in driver_urls
    authority = float(article.get("_authority") or 1)
    if not is_driver and authority < 4:
        # Low-authority aggregator stories can still appear when they are direct
        # market drivers, but they do not fill ordinary sector headline slots.
        return -100.0
    age = _article_age_hours(article, now)
    max_age = MAX_NEWS_LOOKBACK_HOURS if is_driver else SECTION_NEWS_LOOKBACK_HOURS
    if age is None or age < -1 or age > max_age:
        return -100.0
    sector = primary_sector(article)
    if not sector:
        return -100.0
    text = _article_text(article)
    headline = _clean_markup(article.get("headline") or "")
    if not is_driver:
        if CORE_NEWS_NOISE_RE.search(headline) or headline.rstrip().endswith("?"):
            return -100.0
        if sector == "broad_market":
            direct_sector_signal = bool(US_MARKET_RE.search(headline))
        else:
            direct_sector_signal = any(
                re.search(pattern, headline, re.IGNORECASE)
                for pattern in SECTOR_PATTERNS[sector]
            )
        if not direct_sector_signal:
            return -100.0

    score = authority
    if age <= 8:
        score += 5
    elif age <= 24:
        score += 3
    else:
        score += 1
    score += min(_sector_scores(article).get(sector, 0), 10)
    if is_driver:
        score += 15
    if MARKET_RECAP_RE.search(headline):
        score += 4
    if US_MARKET_RE.search(text):
        score += 2
    if LOW_VALUE_RE.search(text):
        score -= 8
    if "/opinion/" in url.lower():
        score -= 8
    return score


def build_sector_groups(
    articles: list[dict],
    drivers: list[dict],
    now: datetime,
    exclude_urls: set[str] | None = None,
) -> list[SectorGroup]:
    """Return a small set of sector buckets containing only core stories."""
    excluded = exclude_urls or set()
    driver_urls = {article.get("url") or "" for article in drivers}
    candidates: list[tuple[float, dict]] = []
    for article in articles:
        if (article.get("url") or "") in excluded:
            continue
        score = _sector_article_score(article, now, driver_urls)
        if score < 10:
            continue
        chosen = dict(article)
        chosen["_sector_key"] = primary_sector(article)
        chosen["_sector_score"] = round(score, 1)
        chosen["_is_driver"] = (article.get("url") or "") in driver_urls
        candidates.append((score, chosen))

    candidates.sort(key=lambda item: item[0], reverse=True)
    grouped: defaultdict[str, list[dict]] = defaultdict(list)
    source_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    for _, article in candidates:
        key = article.get("_sector_key")
        if not key or len(grouped[key]) >= MAX_SECTOR_ARTICLES:
            continue
        source = _source_identity(article)
        if source_counts[key][source] >= 1:
            continue
        if any(_too_similar(article, existing) for existing in grouped[key]):
            continue
        grouped[key].append(article)
        source_counts[key][source] += 1

    ranked_keys = sorted(
        grouped,
        key=lambda key: max(
            float(article.get("_sector_score") or 0) for article in grouped[key]
        ),
        reverse=True,
    )[:MAX_SECTOR_GROUPS]
    return [
        SectorGroup(
            key=key,
            label_zh=SECTOR_LABELS_ZH[key],
            proxy_symbol=SECTOR_PROXIES.get(key),
            articles=grouped[key],
        )
        for key in ranked_keys
    ]


def _matches_special(article: dict, spec: SpecialWatchSpec) -> bool:
    symbols = {str(symbol).upper() for symbol in article.get("symbols") or []}
    if spec.symbol in symbols:
        return True
    text = _article_text(article)
    return any(
        re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
        for keyword in spec.keywords
    )


def select_special_news(
    articles: list[dict],
    spec: SpecialWatchSpec,
    now: datetime,
    limit: int = MAX_SPECIAL_ARTICLES,
) -> list[dict]:
    scored: list[tuple[float, dict]] = []
    for article in articles:
        if article.get("_section") != "market" or not _matches_special(article, spec):
            continue
        age = _article_age_hours(article, now)
        if age is None or age < -1 or age > SECTION_NEWS_LOOKBACK_HOURS:
            continue
        headline = _clean_markup(article.get("headline") or "")
        symbols = {str(symbol).upper() for symbol in article.get("symbols") or []}
        score = float(article.get("_authority") or 1)
        score += 5 if age <= 12 else 3 if age <= 24 else 1
        if spec.symbol in symbols:
            score += 7
        if any(keyword.lower() in headline.lower() for keyword in spec.keywords):
            score += 6
        if LOW_VALUE_RE.search(_article_text(article)):
            score -= 4
        chosen = dict(article)
        chosen["_special_symbol"] = spec.symbol
        chosen["_special_score"] = round(score, 1)
        scored.append((score, chosen))

    scored.sort(key=lambda item: item[0], reverse=True)
    selected: list[dict] = []
    per_source: Counter[str] = Counter()
    for score, article in scored:
        if score < 8:
            continue
        source = _source_identity(article)
        if per_source[source] >= 1:
            continue
        if any(_too_similar(article, existing) for existing in selected):
            continue
        selected.append(article)
        per_source[source] += 1
        if len(selected) >= limit:
            break
    return selected


def unique_articles(groups: list[list[dict]]) -> list[dict]:
    unique: list[dict] = []
    seen: set[str] = set()
    for group in groups:
        for article in group:
            key = article.get("url") or article.get("headline") or ""
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(article)
    return unique


def score_world_article(article: dict, now: datetime) -> float:
    score = float(article.get("_authority") or 1)
    created = parse_ts(article.get("created_at"))
    if created:
        age_hours = (now - created).total_seconds() / 3600
        if age_hours <= 8:
            score += 4
        elif age_hours <= 24:
            score += 2
    text = _article_text(article)
    if WORLD_IMPORTANCE_RE.search(text):
        score += 3
    if LOW_VALUE_RE.search(text) or "new video loaded" in text:
        score -= 5
    return score


def select_world_articles(
    articles: list[dict],
    now: datetime,
    limit: int = MAX_WORLD_ARTICLES,
) -> list[dict]:
    scored = sorted(
        ((score_world_article(article, now), article) for article in articles),
        key=lambda pair: pair[0],
        reverse=True,
    )
    selected: list[dict] = []
    per_source: Counter[str] = Counter()
    for score, article in scored:
        if score <= 0:
            continue
        source = _source_identity(article)
        if per_source[source] >= 2:
            continue
        if any(_too_similar(article, existing) for existing in selected):
            continue
        selected.append(dict(article))
        per_source[source] += 1
        if len(selected) >= limit:
            break
    return selected


def extract_lead(url: str) -> str:
    if not url:
        return ""
    try:
        response = requests.get(url, headers=ARTICLE_FETCH_HEADERS, timeout=12)
        response.raise_for_status()
    except Exception as exc:
        print(f"  Body fetch failed for {url}: {exc}", file=sys.stderr)
        return ""

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "footer", "aside", "header", "form"]):
        tag.decompose()
    container = soup.find("article") or soup.find("main") or soup.body or soup
    paragraphs = [
        paragraph.get_text(" ", strip=True)
        for paragraph in container.find_all("p")
    ]
    return _trim_summary(" ".join(paragraphs))


def translate_zh(text: str) -> str:
    if not text.strip():
        return ""
    try:
        response = requests.get(
            GTRANS_URL,
            params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=8,
        )
        response.raise_for_status()
        payload = response.json()
        return "".join(segment[0] for segment in payload[0] if segment and segment[0]).strip()
    except Exception as exc:
        print(f"  Translation failed: {exc}", file=sys.stderr)
        return ""


def enrich_articles(articles: list[dict]) -> None:
    """Create short bilingual summaries only after evidence-based selection."""
    for index, article in enumerate(articles, 1):
        headline = _clean_markup(article.get("headline") or "")
        supplied = article.get("content") or article.get("summary") or ""
        summary = _trim_summary(supplied)
        if len(summary) < 100 or BOILERPLATE_RE.search(summary):
            summary = extract_lead(article.get("url") or "") or summary
        article["_summary"] = summary
        if not article.get("_zh_headline"):
            print(f"  [{index}/{len(articles)}] translating: {headline[:64]}...")
            article["_zh_headline"] = translate_zh(headline)
        if not article.get("_zh_summary"):
            article["_zh_summary"] = translate_zh(summary) if summary else ""


def _html_escape(value: str) -> str:
    return html.escape(value or "", quote=True)


def _safe_url(value: str) -> str:
    parsed = urlparse(value or "")
    return value if parsed.scheme in {"http", "https"} else "#"


def _format_article_time(article: dict) -> str:
    created = parse_ts(article.get("created_at"))
    if not created:
        return "时间未提供"
    return created.astimezone(NY_TZ).strftime("%a %-m/%-d %-I:%M %p ET")


def _article_html(article: dict, index: int) -> str:
    headline = _clean_markup(article.get("headline") or "")
    source = (article.get("source") or "来源未提供").strip()
    url = _safe_url(article.get("url") or "")
    summary = article.get("_summary") or ""
    zh_headline = article.get("_zh_headline") or ""
    zh_summary = article.get("_zh_summary") or ""
    symbols = article.get("symbols") or []
    symbol_text = f" · {', '.join(symbols[:5])}" if symbols else ""
    badge_html = (
        '<span class="story-badge">核心驱动</span>'
        if article.get("_is_driver") else ""
    )
    zh_headline_html = (
        f'<div class="zh-headline">{_html_escape(zh_headline)}</div>'
        if zh_headline else ""
    )
    summary_html = f'<p class="summary">{_html_escape(summary)}</p>' if summary else ""
    zh_summary_html = (
        f'<p class="zh-summary">{_html_escape(zh_summary)}</p>' if zh_summary else ""
    )
    return (
        '<article class="story">\n'
        f'  <div class="story-number">{index:02d}</div>\n'
        '  <div class="story-content">\n'
        f'    <div class="meta">{_html_escape(source)} · '
        f'{_html_escape(_format_article_time(article))}{_html_escape(symbol_text)}'
        f'{badge_html}</div>\n'
        f'    <h3><a href="{_html_escape(url)}" target="_blank" rel="noopener noreferrer">'
        f'{_html_escape(headline)}</a></h3>\n'
        f'    {zh_headline_html}\n'
        f'    {summary_html}\n'
        f'    {zh_summary_html}\n'
        '  </div>\n'
        '</article>'
    )


def _format_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _sector_move_label_zh(move: MarketMove) -> str:
    for key, symbol in SECTOR_PROXIES.items():
        if symbol == move.symbol:
            return SECTOR_LABELS_ZH[key]
    return move.label


def _overview_html(overview: MarketOverview) -> str:
    evidence = "".join(
        f"<li>{_html_escape(item)}</li>" for item in overview.evidence_zh
    )
    return (
        f'<div class="overview tone-{_html_escape(overview.tone)}">'
        '<div class="overview-kicker">今日总览</div>'
        f'<div class="overview-label">{_html_escape(overview.label_zh)}</div>'
        f'<p>{_html_escape(overview.summary_zh)}</p>'
        f'<ul>{evidence}</ul>'
        '</div>'
    )


def _market_section_html(
    pulse: MarketPulse,
    assessment: DriverAssessment,
    overview: MarketOverview,
) -> str:
    if not pulse.available:
        return (
            '<section class="market-pulse"><div class="section-kicker">MARKET PULSE</div>'
            '<h2>行情数据不足</h2>'
            f'<p class="pulse-summary">{_html_escape(pulse.note)}</p>'
            f'{_overview_html(overview)}'
            '<p class="confidence">未生成涨跌原因。</p></section>'
        )

    broad_cards: list[str] = []
    for symbol in BROAD_MARKET_SYMBOLS:
        move = pulse.moves.get(symbol)
        if not move:
            continue
        css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
        broad_cards.append(
            '<div class="move">'
            f'<div class="move-label">{_html_escape(move.label)}</div>'
            f'<div class="move-value {css_class}">{_format_pct(move.change_pct)}</div>'
            f'<div class="move-close">ETF {symbol} · {move.close:,.2f}</div>'
            '</div>'
        )

    sector_moves = sorted(
        (move for move in pulse.moves.values() if move.group == "sector"),
        key=lambda move: move.change_pct,
        reverse=True,
    )
    sector_line = ""
    if sector_moves:
        leader = sector_moves[0]
        laggard = sector_moves[-1]
        sector_line = (
            '<p class="sector-line">'
            f'领涨：{_html_escape(_sector_move_label_zh(leader))} '
            f'{_format_pct(leader.change_pct)} · '
            f'落后：{_html_escape(_sector_move_label_zh(laggard))} '
            f'{_format_pct(laggard.change_pct)}'
            '</p>'
        )

    session = pulse.session_date.strftime("%A, %B %-d, %Y")
    return (
        '<section class="market-pulse">'
        '<div class="section-kicker">MARKET PULSE</div>'
        f'<div class="session-date">最近完整交易日 · {_html_escape(session)}</div>'
        f'<h2>{_html_escape(_pulse_label_zh(pulse.status))}</h2>'
        f'<div class="moves">{"".join(broad_cards)}</div>'
        f'{sector_line}'
        f'{_overview_html(overview)}'
        '<div class="explanation">'
        f'<div class="confidence">新闻归因 · {_html_escape(assessment.confidence)}置信度</div>'
        f'<p>{_html_escape(assessment.summary_zh)}</p>'
        '</div>'
        f'<p class="data-note">数据：{_html_escape(pulse.note)}。ETF 用作指数代理；'
        '新闻归因基于同日来源和方向一致性，不是可证明的唯一因果。</p>'
        '</section>'
    )


def _sector_groups_html(groups: list[SectorGroup], pulse: MarketPulse) -> str:
    if not groups:
        return (
            '<section class="sector-news"><div class="section-kicker">SECTOR MAP</div>'
            '<h2>板块核心新闻</h2>'
            '<p class="empty">当前窗口没有达到核心新闻门槛的板块报道。</p></section>'
        )

    blocks: list[str] = []
    story_number = 1
    for group in groups:
        proxy_html = ""
        move = pulse.moves.get(group.proxy_symbol or "")
        if move:
            css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
            proxy_html = (
                f'<span class="sector-move {css_class}">{move.symbol} '
                f'{_format_pct(move.change_pct)}</span>'
            )
        stories: list[str] = []
        for article in group.articles:
            stories.append(_article_html(article, story_number))
            story_number += 1
        blocks.append(
            '<div class="sector-block">'
            f'<div class="sector-heading"><h3>{_html_escape(group.label_zh)}</h3>'
            f'{proxy_html}</div>'
            f'{"".join(stories)}'
            '</div>'
        )
    return (
        '<section class="sector-news"><div class="section-kicker">SECTOR MAP</div>'
        '<h2>板块核心新闻</h2>'
        '<p class="section-note">每个板块最多两条；优先同日市场驱动和高质量来源。</p>'
        f'{"".join(blocks)}</section>'
    )


def _special_watch_html(
    spec: SpecialWatchSpec,
    pulse: MarketPulse,
    articles: list[dict],
) -> str:
    move = pulse.moves.get(spec.symbol)
    if move:
        css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
        move_html = (
            '<div class="special-quote">'
            f'<span class="special-price">{move.close:,.2f}</span>'
            f'<span class="special-change {css_class}">{_format_pct(move.change_pct)}</span>'
            '</div>'
        )
    else:
        move_html = '<div class="special-quote unavailable">行情暂不可用</div>'

    if articles:
        stories = "".join(
            _article_html(article, index)
            for index, article in enumerate(articles, 1)
        )
    else:
        stories = '<p class="empty">最近 36 小时没有达到门槛的专项新闻。</p>'

    note = (
        '<p class="special-note">同时匹配 SPCX 代码及 SpaceX、Starlink、Starship 关键词；'
        '相关报道不自动等同于 SPCX 的直接基本面。</p>'
        if spec.symbol == "SPCX" else
        '<p class="special-note">独立跟踪 MRVL 行情、Marvell 公司新闻和半导体催化剂。</p>'
    )
    return (
        '<div class="special-block">'
        f'<div class="special-heading"><h3>{_html_escape(spec.label_zh)}</h3>{move_html}</div>'
        f'{note}{stories}</div>'
    )


def _special_sections_html(
    pulse: MarketPulse,
    special_news: dict[str, list[dict]],
) -> str:
    blocks = "".join(
        _special_watch_html(spec, pulse, special_news.get(symbol, []))
        for symbol, spec in SPECIAL_WATCH_SPECS.items()
    )
    return (
        '<section class="special-watch"><div class="section-kicker">FOCUS LIST</div>'
        '<h2>重点标的独立跟踪</h2>'
        f'<div class="special-grid">{blocks}</div></section>'
    )


def _stories_section_html(title: str, articles: list[dict], empty_text: str) -> str:
    if not articles:
        return (
            '<section class="stories"><div class="section-kicker">EVIDENCE</div>'
            f'<h2>{_html_escape(title)}</h2>'
            f'<p class="empty">{_html_escape(empty_text)}</p></section>'
        )
    items = "\n".join(_article_html(article, index) for index, article in enumerate(articles, 1))
    return (
        '<section class="stories"><div class="section-kicker">EVIDENCE</div>'
        f'<h2>{_html_escape(title)}</h2>{items}</section>'
    )


HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN" data-generated-at="$generated_iso">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#0b0c0e">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Market Brief">
<title>Market Brief — $date_short</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="icon.svg">
<link rel="apple-touch-icon" href="icon.svg">
<style>
  :root {
    --bg: #0b0c0e; --surface: #121418; --surface-2: #181b20;
    --ink: #f3f1eb; --muted: #979ba3; --line: #282c33;
    --green: #5ee39a; --red: #ff7c82; --amber: #e7bd72;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); }
  body {
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    line-height: 1.58; padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }
  .wrap { width: min(880px, 100%); margin: 0 auto; padding: 0 22px; }
  header { padding: 40px 0 26px; border-bottom: 1px solid var(--line); }
  .eyebrow, .section-kicker {
    color: var(--amber); font-size: 11px; font-weight: 750; letter-spacing: .16em;
  }
  h1 { margin: 7px 0 0; font-size: clamp(32px, 7vw, 54px); line-height: 1.05; letter-spacing: -.04em; }
  .date { color: var(--muted); margin-top: 10px; font-size: 14px; }
  .refresh-status { display: flex; align-items: center; gap: 7px; color: var(--muted); margin-top: 8px; font-size: 12px; }
  .refresh-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 3px rgba(94, 227, 154, .12); }
  section { padding: 34px 0; border-bottom: 1px solid var(--line); }
  h2 { margin: 7px 0 20px; font-size: 25px; letter-spacing: -.02em; }
  .session-date, .data-note, .confidence, .meta, .move-close, .sector-line, .empty {
    color: var(--muted); font-size: 12px;
  }
  .moves { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .move { padding: 15px; border: 1px solid var(--line); background: var(--surface); border-radius: 10px; }
  .move-label { color: var(--muted); font-size: 12px; }
  .move-value { margin: 3px 0 1px; font-size: 22px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .up { color: var(--green); } .down { color: var(--red); } .flat { color: var(--ink); }
  .sector-line { margin: 13px 0 0; }
  .explanation { margin-top: 22px; padding: 19px 20px; background: var(--surface-2); border-left: 3px solid var(--amber); }
  .explanation p { margin: 7px 0 0; font-size: 16px; }
  .overview { margin-top: 22px; padding: 20px; background: var(--surface-2); border: 1px solid var(--line); border-left-width: 4px; }
  .overview-kicker { color: var(--muted); font-size: 11px; letter-spacing: .13em; }
  .overview-label { margin-top: 4px; font-size: 25px; font-weight: 780; letter-spacing: -.02em; }
  .overview p { margin: 8px 0 9px; }
  .overview ul { margin: 0; padding-left: 19px; color: var(--muted); font-size: 13px; }
  .tone-positive, .tone-cautious-positive { border-left-color: var(--green); }
  .tone-negative, .tone-cautious-negative { border-left-color: var(--red); }
  .tone-neutral, .tone-unavailable { border-left-color: var(--amber); }
  .data-note { margin: 14px 0 0; }
  .section-note, .special-note { color: var(--muted); font-size: 12px; }
  .sector-block { margin-top: 26px; }
  .sector-heading, .special-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; border-bottom: 1px solid var(--line); }
  .sector-heading h3, .special-heading h3 { margin: 0 0 9px; font-size: 20px; }
  .sector-move { font-size: 13px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .story { display: grid; grid-template-columns: 36px 1fr; gap: 10px; padding: 20px 0; border-top: 1px solid var(--line); }
  .sector-block .story:first-of-type { border-top: 0; }
  .story-number { color: #555b65; font-size: 12px; padding-top: 2px; }
  .story h3 { margin: 6px 0 4px; font-size: 18px; line-height: 1.35; }
  .story h3 a { color: var(--ink); text-decoration: none; }
  .story h3 a:hover { color: var(--green); }
  .zh-headline { color: #c5c8ce; font-size: 15px; }
  .story-badge { margin-left: 8px; color: var(--amber); font-size: 10px; letter-spacing: .08em; }
  .summary { margin: 13px 0 4px; color: #d9dbe0; font-size: 14px; }
  .zh-summary { margin: 5px 0 0; color: var(--muted); font-size: 14px; }
  .special-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; }
  .special-block { min-width: 0; padding: 18px; background: var(--surface); border: 1px solid var(--line); border-radius: 10px; }
  .special-quote { display: flex; align-items: baseline; gap: 8px; white-space: nowrap; }
  .special-price { font-size: 19px; font-weight: 750; }
  .special-change { font-size: 13px; font-weight: 750; }
  .special-quote.unavailable { color: var(--muted); font-size: 12px; }
  .special-block .story { grid-template-columns: 24px 1fr; }
  .special-block .story h3 { font-size: 16px; }
  .special-block .summary, .special-block .zh-summary { display: none; }
  footer { padding: 28px 0 40px; color: var(--muted); font-size: 11px; }
  @media (max-width: 680px) {
    .wrap { padding: 0 16px; }
    header { padding-top: 30px; }
    .moves { grid-template-columns: repeat(2, 1fr); }
    .special-grid { grid-template-columns: 1fr; }
    .move-value { font-size: 20px; }
    .story { grid-template-columns: 28px 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">EVIDENCE-FIRST · US MARKETS</div>
    <h1>Market Brief</h1>
    <div class="date">$date_long · $mode_label</div>
    <div class="refresh-status"><span class="refresh-dot" aria-hidden="true"></span><span id="refresh-status-text">网页自动更新 · 每分钟检查</span></div>
  </header>
  $market_section
  $sector_section
  $special_section
  $world_section
  <footer>Generated $generated_et · America/New_York<br>
  行情可能延迟；利好/利空判断基于最近完整交易日和同日新闻证据，不构成投资建议。</footer>
</div>
<script>
(() => {
  const currentVersion = document.documentElement.dataset.generatedAt;
  const statusText = document.getElementById("refresh-status-text");

  async function checkForUpdate() {
    try {
      const response = await fetch("status.json?ts=" + Date.now(), { cache: "no-store" });
      if (!response.ok) return;
      const status = await response.json();
      if (status.generated_at && status.generated_at !== currentVersion) {
        statusText.textContent = "发现新简报，正在刷新…";
        const nextUrl = new URL(window.location.href);
        nextUrl.searchParams.set("v", status.generated_at);
        window.location.replace(nextUrl.toString());
      }
    } catch (_error) {
      // Keep the current brief visible when the network is temporarily unavailable.
    }
  }

  checkForUpdate();
  window.setInterval(checkForUpdate, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) checkForUpdate();
  });
})();
</script>
</body>
</html>
""")

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="42" fill="#0b0c0e"/>
  <path d="M34 126 L67 97 L91 110 L126 66 L159 82" fill="none" stroke="#5ee39a" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="126" cy="66" r="9" fill="#e7bd72"/>
</svg>
"""

MANIFEST_JSON = """{
  "name": "Evidence-First Market Brief",
  "short_name": "Market Brief",
  "description": "Daily US market moves with evidence-ranked explanations",
  "start_url": ".",
  "scope": ".",
  "display": "standalone",
  "background_color": "#0b0c0e",
  "theme_color": "#0b0c0e",
  "icons": [
    { "src": "icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any" }
  ]
}
"""


def _brief_mode(now: datetime) -> str:
    return "收盘复盘" if now.astimezone(NY_TZ).time() >= time(16, 30) else "晨间简报"


def render_html(
    pulse: MarketPulse,
    assessment: DriverAssessment,
    world_articles: list[dict],
    output_dir: Path,
    now: datetime,
    overview: MarketOverview | None = None,
    sector_groups: list[SectorGroup] | None = None,
    special_news: dict[str, list[dict]] | None = None,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    local = now.astimezone(NY_TZ)
    overview = overview or build_market_overview(pulse, assessment)
    sector_groups = sector_groups if sector_groups is not None else build_sector_groups(
        assessment.articles,
        assessment.articles,
        now,
    )
    special_news = special_news or {symbol: [] for symbol in SPECIAL_WATCH_SPECS}
    page = HTML_TEMPLATE.safe_substitute(
        date_short=local.strftime("%-m/%-d"),
        date_long=local.strftime("%A · %B %-d, %Y"),
        mode_label=_brief_mode(now),
        generated_iso=now.astimezone(timezone.utc).isoformat(),
        generated_et=local.strftime("%Y-%m-%d %-I:%M %p ET"),
        market_section=_market_section_html(pulse, assessment, overview),
        sector_section=_sector_groups_html(sector_groups, pulse),
        special_section=_special_sections_html(pulse, special_news),
        world_section=_stories_section_html(
            "全球重大新闻",
            world_articles,
            "当前窗口没有达到筛选门槛的全球新闻。",
        ),
    )
    index_path = output_dir / "index.html"
    index_path.write_text(page, encoding="utf-8")
    status = {
        "generated_at": now.astimezone(timezone.utc).isoformat(),
        "generated_et": local.strftime("%Y-%m-%d %-I:%M %p ET"),
        "market_session": pulse.session_date.isoformat() if pulse.session_date else None,
        "overview": overview.label_zh,
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "manifest.webmanifest").write_text(MANIFEST_JSON, encoding="utf-8")
    (output_dir / "icon.svg").write_text(ICON_SVG, encoding="utf-8")
    return index_path


def _notification_messages(
    pulse: MarketPulse,
    assessment: DriverAssessment,
    world_articles: list[dict],
    now: datetime,
    overview: MarketOverview | None = None,
    sector_groups: list[SectorGroup] | None = None,
    special_news: dict[str, list[dict]] | None = None,
) -> list[tuple[str, str, str | None]]:
    local = now.astimezone(NY_TZ)
    mode = "Market Close" if _brief_mode(now) == "收盘复盘" else "Morning Brief"
    overview = overview or build_market_overview(pulse, assessment)
    sector_groups = sector_groups or []
    special_news = special_news or {symbol: [] for symbol in SPECIAL_WATCH_SPECS}
    title = f"{mode} · {overview.label_zh} — {local.strftime('%a %-m/%-d')}"
    lines: list[str] = []
    if pulse.available:
        session = pulse.session_date.strftime("%-m/%-d") if pulse.session_date else ""
        moves = "  ".join(
            f"{symbol} {_format_pct(pulse.moves[symbol].change_pct)}"
            for symbol in BROAD_MARKET_SYMBOLS
            if symbol in pulse.moves
        )
        lines.extend(
            [
                f"【今日总览】{overview.label_zh}",
                overview.summary_zh,
                f"最近完整交易日 {session}: {moves}",
            ]
        )
        lines.extend(f"• {item}" for item in overview.evidence_zh[:2])
    else:
        lines.extend([f"【今日总览】{overview.label_zh}", overview.summary_zh])

    lines.append("【重点标的】")
    for symbol, spec in SPECIAL_WATCH_SPECS.items():
        move = pulse.moves.get(symbol)
        move_text = (
            f"${move.close:,.2f} {_format_pct(move.change_pct)}"
            if move else "行情暂不可用"
        )
        articles = special_news.get(symbol, [])
        lines.append(f"▸ {spec.label_zh}: {move_text}")
        if articles:
            article = articles[0]
            headline = article.get("_zh_headline") or article.get("headline") or ""
            lines.append(f"- {headline} — {article.get('source') or '未知来源'}")
            if article.get("url"):
                lines.append(article["url"])
        else:
            lines.append("- 最近 36 小时无达到门槛的专项新闻")

    if sector_groups:
        lines.append("【板块核心新闻】")
        for group in sector_groups:
            move = pulse.moves.get(group.proxy_symbol or "")
            move_text = f" {move.symbol} {_format_pct(move.change_pct)}" if move else ""
            lines.append(f"▸ {group.label_zh}{move_text}")
            article = group.articles[0]
            headline = article.get("_zh_headline") or article.get("headline") or ""
            lines.append(f"- {headline} — {article.get('source') or '未知来源'}")
            if article.get("url"):
                lines.append(article["url"])

    click_url = next(
        (
            article.get("url")
            for group in sector_groups
            for article in group.articles
            if article.get("url")
        ),
        assessment.articles[0].get("url") if assessment.articles else None,
    )
    messages = [(title, "\n".join(lines), click_url)]
    if world_articles:
        world_lines = []
        for index, article in enumerate(world_articles, 1):
            headline = article.get("_zh_headline") or article.get("headline") or ""
            world_lines.append(
                f"{index}. {headline} — {article.get('source') or '未知来源'}\n"
                f"{article.get('url') or ''}"
            )
        messages.append(
            (
                f"World News — {local.strftime('%a %-m/%-d')}",
                "\n\n".join(world_lines),
                world_articles[0].get("url"),
            )
        )
    return messages


def push_ntfy(topic: str, title: str, body: str, click_url: str | None) -> str | None:
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": "chart_with_upwards_trend",
    }
    if click_url:
        headers["Click"] = click_url
    response = requests.post(
        f"{NTFY_URL}/{topic}",
        data=_truncate_utf8(body, 4000).encode("utf-8"),
        headers=headers,
        timeout=15,
    )
    response.raise_for_status()
    try:
        return response.json().get("id")
    except (ValueError, AttributeError):
        return None


def _truncate_utf8(value: str, byte_limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= byte_limit:
        return value
    return encoded[:byte_limit].decode("utf-8", errors="ignore").rstrip()


def scheduled_slot(now: datetime) -> str | None:
    """Return the valid ET slot; duplicate DST cron invocations return None."""
    local = now.astimezone(NY_TZ)
    if local.weekday() >= 5:
        return None
    minute_of_day = local.hour * 60 + local.minute
    if 8 * 60 + 45 <= minute_of_day <= 9 * 60 + 55:
        return "morning"
    if 16 * 60 + 30 <= minute_of_day <= 17 * 60 + 20:
        return "close"
    return None


def web_refresh_allowed(now: datetime) -> bool:
    """Limit high-frequency page refreshes to weekday US-market hours."""
    local = now.astimezone(NY_TZ)
    if local.weekday() >= 5:
        return False
    minute_of_day = local.hour * 60 + local.minute
    return 8 * 60 + 45 <= minute_of_day <= 18 * 60 + 15


def news_window(pulse: MarketPulse, now: datetime) -> tuple[datetime, datetime]:
    start = now - timedelta(hours=NEWS_LOOKBACK_HOURS)
    if pulse.session_date:
        session_start_et = datetime.combine(pulse.session_date, time.min, tzinfo=NY_TZ)
        start = min(start, session_start_et.astimezone(timezone.utc))
    floor = now - timedelta(hours=MAX_NEWS_LOOKBACK_HOURS)
    return max(start, floor), now


def _demo_payload(now: datetime) -> tuple[MarketPulse, DriverAssessment, list[dict]]:
    session = now.astimezone(NY_TZ).date() - timedelta(days=1)
    while session.weekday() >= 5:
        session -= timedelta(days=1)
    demo_moves = {
        "SPY": MarketMove("SPY", "S&P 500", 604.2, 611.5, -1.19, session, "broad"),
        "QQQ": MarketMove("QQQ", "Nasdaq 100", 515.4, 526.8, -2.16, session, "broad"),
        "DIA": MarketMove("DIA", "Dow", 445.2, 447.4, -0.49, session, "broad"),
        "IWM": MarketMove("IWM", "Russell 2000", 221.0, 224.1, -1.38, session, "broad"),
        "SMH": MarketMove("SMH", "Semiconductors", 281.0, 296.0, -5.07, session, "sector"),
        "XLF": MarketMove("XLF", "Financials", 52.1, 52.0, 0.19, session, "sector"),
        "MRVL": MarketMove("MRVL", "Marvell", 88.4, 92.1, -4.02, session, "special"),
        "SPCX": MarketMove("SPCX", "SPCX", 112.3, 110.8, 1.35, session, "special"),
    }
    pulse = MarketPulse(session, demo_moves, "strong_down", "sip", "演示用延迟 SIP 日线")
    created = datetime.combine(session, time(21, 0), tzinfo=timezone.utc).isoformat()
    articles = [
        {
            "headline": "Wall Street falls as hot jobs data lifts rate expectations and chips slide",
            "summary": "A stronger jobs report pushed Treasury yields higher while semiconductor shares led the decline.",
            "content": "A stronger jobs report pushed Treasury yields higher while semiconductor shares led the decline.",
            "url": "https://example.com/market-wrap",
            "source": "Demo Market Wire",
            "symbols": ["SPY", "QQQ", "NVDA"],
            "created_at": created,
            "_section": "market",
            "_authority": 6,
            "_impact_score": 30.0,
            "_categories": ["inflation_jobs", "rates", "ai_chips"],
            "_is_recap": True,
            "_summary": "A stronger jobs report pushed Treasury yields higher while semiconductor shares led the decline.",
            "_zh_headline": "强劲就业数据推高利率预期，芯片股下挫拖累美股",
            "_zh_summary": "强于预期的就业报告推高了美债收益率，半导体股票领跌。",
        },
        {
            "headline": "Chip shares tumble after rate fears return",
            "summary": "Semiconductors posted the market's sharpest sector decline.",
            "content": "Semiconductors posted the market's sharpest sector decline.",
            "url": "https://example.org/chips",
            "source": "Demo Business News",
            "symbols": ["NVDA", "AVGO"],
            "created_at": created,
            "_section": "market",
            "_authority": 5,
            "_impact_score": 22.0,
            "_categories": ["rates", "ai_chips"],
            "_is_recap": False,
            "_summary": "Semiconductors posted the market's sharpest sector decline.",
            "_zh_headline": "利率担忧再起，芯片股大幅下跌",
            "_zh_summary": "半导体成为当日跌幅最大的板块。",
        },
    ]
    assessment = build_driver_assessment(pulse, articles)
    return pulse, assessment, []


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-push", action="store_true", help="Render without sending ntfy notifications")
    parser.add_argument("--scheduled", action="store_true", help="Apply ET/DST schedule gate")
    parser.add_argument(
        "--web-refresh",
        action="store_true",
        help="Apply the weekday ET market-hours gate for silent website updates",
    )
    parser.add_argument("--demo", action="store_true", help="Render clearly labeled synthetic data without network access")
    parser.add_argument(
        "--placeholder",
        action="store_true",
        help="Render an honest waiting-for-live-data page without network access",
    )
    parser.add_argument("--output-dir", type=Path, default=SITE_DIR, help="HTML output directory")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now = datetime.now(timezone.utc)

    if args.demo:
        pulse, assessment, world_articles = _demo_payload(now)
        path = render_html(pulse, assessment, world_articles, args.output_dir, now)
        print(f"Rendered demo site: {path}")
        return 0

    if args.placeholder:
        pulse = MarketPulse(
            session_date=None,
            note="代码已升级；等待下一次使用有效 Alpaca 凭据生成经过验证的行情与新闻。",
        )
        assessment = build_driver_assessment(pulse, [])
        path = render_html(pulse, assessment, [], args.output_dir, now)
        print(f"Rendered live-data placeholder: {path}")
        return 0

    slot = scheduled_slot(now) if args.scheduled else None
    if args.scheduled and slot is None:
        print(f"Skipping duplicate/out-of-window scheduled invocation at {now.astimezone(NY_TZ).isoformat()}")
        return 0
    if args.web_refresh and not web_refresh_allowed(now):
        print(f"Skipping out-of-window website refresh at {now.astimezone(NY_TZ).isoformat()}")
        return 0

    env = load_env(ENV_PATH)
    api_key = env.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
    api_secret = env.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET")
    topic = env.get("NTFY_NEWS_TOPIC") or os.environ.get("NTFY_NEWS_TOPIC")
    missing = [
        name
        for name, value in (
            ("ALPACA_API_KEY", api_key),
            ("ALPACA_API_SECRET", api_secret),
            ("NTFY_NEWS_TOPIC", topic if not args.no_push else "not-required"),
        )
        if not value
    ]
    if missing:
        print(f"ERROR: missing env vars: {', '.join(missing)}", file=sys.stderr)
        return 2

    pulse = fetch_market_pulse(api_key, api_secret, now)
    print(
        "Market pulse:",
        pulse.session_date.isoformat() if pulse.session_date else "unavailable",
        pulse.status,
        pulse.feed,
    )

    if (
        args.scheduled
        and slot == "close"
        and pulse.available
        and pulse.session_date != now.astimezone(NY_TZ).date()
    ):
        print("No completed US session today; skipping duplicate holiday close brief.")
        return 0

    start, end = news_window(pulse, now)
    print(f"Fetching news from {start.isoformat()} to {end.isoformat()}")

    market_articles: list[dict] = []
    try:
        alpaca_articles = fetch_alpaca_news(api_key, api_secret, start, end)
        market_articles.extend(alpaca_articles)
        print(f"Fetched {len(alpaca_articles)} market articles from Alpaca/Benzinga")
    except Exception as exc:
        print(f"  Alpaca news fetch failed: {exc}", file=sys.stderr)

    for spec in MARKET_FEEDS:
        items = fetch_rss(spec, start, end)
        print(f"Fetched {len(items)} from {spec.source}")
        market_articles.extend(items)

    world_candidates: list[dict] = []
    for spec in WORLD_FEEDS:
        items = fetch_rss(spec, start, end)
        print(f"Fetched {len(items)} from {spec.source}")
        world_candidates.extend(items)

    drivers = select_market_drivers(market_articles, pulse)
    world_articles = select_world_articles(world_candidates, now)
    assessment = build_driver_assessment(pulse, drivers)
    overview = build_market_overview(pulse, assessment)
    special_news = {
        symbol: select_special_news(market_articles, spec, now)
        for symbol, spec in SPECIAL_WATCH_SPECS.items()
    }
    special_urls = {
        article.get("url") or ""
        for articles in special_news.values()
        for article in articles
    }
    sector_groups = build_sector_groups(
        market_articles,
        drivers,
        now,
        exclude_urls=special_urls,
    )
    sector_articles = [
        article for group in sector_groups for article in group.articles
    ]
    selected = unique_articles(
        [
            sector_articles,
            *special_news.values(),
            world_articles,
            drivers,
        ]
    )
    enrich_articles(selected)

    print(
        f"Selected {len(drivers)} market drivers, {len(sector_groups)} sector groups, "
        f"{sum(len(items) for items in special_news.values())} special-watch stories, "
        f"and {len(world_articles)} world stories"
    )
    print(f"Overview: {overview.label_zh} — {overview.summary_zh}")
    for article in drivers:
        print(
            f"  market [{article.get('_impact_score')}] "
            f"{article.get('source')}: {article.get('headline')}"
        )

    for group in sector_groups:
        print(
            f"  sector {group.label_zh}: "
            + " | ".join(article.get("headline") or "" for article in group.articles)
        )
    for symbol, articles in special_news.items():
        print(f"  special {symbol}: {len(articles)} stories")

    index_path = render_html(
        pulse,
        assessment,
        world_articles,
        args.output_dir,
        now,
        overview=overview,
        sector_groups=sector_groups,
        special_news=special_news,
    )
    print(f"Rendered site: {index_path}")

    if args.no_push:
        print("ntfy push disabled by --no-push")
        return 0

    delivery_failed = False
    for title, body, click_url in _notification_messages(
        pulse,
        assessment,
        world_articles,
        now,
        overview=overview,
        sector_groups=sector_groups,
        special_news=special_news,
    ):
        try:
            message_id = push_ntfy(topic, title, body, click_url)
            suffix = f" (message_id={message_id})" if message_id else ""
            print(f"Pushed ntfy notification: {title}{suffix}")
        except Exception as exc:
            delivery_failed = True
            print(f"ERROR: ntfy delivery incomplete for {title}: {exc}", file=sys.stderr)
    return 1 if delivery_failed else 0


if __name__ == "__main__":
    sys.exit(main())
