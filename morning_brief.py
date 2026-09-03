#!/usr/bin/env python3
"""Evidence-first US market brief.

The brief first establishes what the broad US market actually did using delayed
consolidated daily bars.  It then ranks same-session reporting that can explain
that move.  News is never presented as a proven cause: source diversity,
timestamp alignment, and confidence are shown explicitly.

The normal scheduled morning run explains the latest completed session.  A
second after-close run can explain the current session once a completed bar is
available.  Both runs also include a small world-news section.  Silent weekend
refreshes publish current news while retaining the latest completed session.
Weekday premarket runs keep the prior close separate from delayed extended-hours
prices and volume.
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
from dataclasses import dataclass, field, replace
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
WEEKLY_EVENTS_PATH = ROOT / "data" / "weekly_events.json"
EVENT_RESULTS_CACHE_NAME = "event-results.json"
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
EVENT_RESULT_LOOKBACK_HOURS = 36
EVENT_RESULT_GRACE_MINUTES = 15
WEB_REFRESH_TARGET_MINUTES = 15
SUMMARY_TARGET_CHARS = 360
SUMMARY_MAX_CHARS = 480
COMPLETED_SESSION_DELAY_MINUTES = 30
MARKET_DATA_DELAY_MINUTES = 16
PREMARKET_START_ET = time(4, 0)
PREMARKET_DISPLAY_START_ET = time(6, 30)
REGULAR_MARKET_OPEN_ET = time(9, 30)

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
PREMARKET_SYMBOLS = ("SPY", "QQQ", "IWM", "SMH", "MRVL", "SPCX")

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


@dataclass(frozen=True)
class PremarketMove:
    symbol: str
    label: str
    price: float
    previous_close: float
    change_pct: float
    volume: int
    high: float
    low: float
    as_of: datetime


@dataclass
class PremarketPulse:
    session_date: date | None
    moves: dict[str, PremarketMove] = field(default_factory=dict)
    status: str = "unavailable"
    feed: str = "unavailable"
    as_of: datetime | None = None
    note: str = "当前不在盘前窗口"

    @property
    def active(self) -> bool:
        return self.session_date is not None

    @property
    def available(self) -> bool:
        return self.active and bool(self.moves)


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


@dataclass(frozen=True)
class PortfolioImpact:
    symbols: tuple[str, ...]
    sensitivity: str
    summary_zh: str


@dataclass(frozen=True)
class EventMetric:
    label_zh: str
    actual: str
    expected: str = ""
    previous: str = ""


@dataclass(frozen=True)
class EventResult:
    published_at: datetime
    source: str
    source_url: str
    verdict_zh: str
    summary_zh: str
    sector_impact_zh: str
    portfolio_impact_zh: str
    tone: str = "neutral"
    metrics: tuple[EventMetric, ...] = ()


@dataclass(frozen=True)
class WeeklyEvent:
    starts_at: datetime
    time_label_zh: str
    title_zh: str
    importance: str
    source: str
    source_url: str
    watch_zh: str
    bullish_if_zh: str
    bearish_if_zh: str
    bullish_sectors: tuple[str, ...]
    bearish_sectors: tuple[str, ...]
    portfolio_impacts: tuple[PortfolioImpact, ...]
    event_id: str = ""
    result_kind: str = ""
    result_terms: tuple[str, ...] = ()
    result: EventResult | None = None


@dataclass
class WeeklyEventCalendar:
    week_start: date
    week_end: date
    events: list[WeeklyEvent] = field(default_factory=list)
    verified_at: datetime | None = None
    portfolio_symbols: tuple[str, ...] = ()
    portfolio_note_zh: str = ""


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


def market_week_dates(now: datetime) -> tuple[date, date]:
    """Return the market week to show; weekends preview the coming week."""
    local_date = now.astimezone(NY_TZ).date()
    weekday = local_date.weekday()
    if weekday >= 5:
        monday = local_date + timedelta(days=7 - weekday)
    else:
        monday = local_date - timedelta(days=weekday)
    return monday, monday + timedelta(days=6)


def _event_result_from_payload(raw: object) -> EventResult | None:
    if not isinstance(raw, dict):
        return None
    published_at = parse_ts(str(raw.get("published_at") or ""))
    source_url = str(raw.get("source_url") or "")
    if published_at is None or urlparse(source_url).scheme not in {"http", "https"}:
        return None
    metrics = tuple(
        EventMetric(
            label_zh=str(item.get("label_zh") or "指标"),
            actual=str(item.get("actual") or ""),
            expected=str(item.get("expected") or ""),
            previous=str(item.get("previous") or ""),
        )
        for item in raw.get("metrics") or []
        if isinstance(item, dict) and str(item.get("actual") or "").strip()
    )
    return EventResult(
        published_at=published_at,
        source=str(raw.get("source") or "来源未提供"),
        source_url=source_url,
        verdict_zh=str(raw.get("verdict_zh") or "结果已公布"),
        summary_zh=str(raw.get("summary_zh") or ""),
        sector_impact_zh=str(raw.get("sector_impact_zh") or ""),
        portfolio_impact_zh=str(raw.get("portfolio_impact_zh") or ""),
        tone=str(raw.get("tone") or "neutral"),
        metrics=metrics,
    )


def _event_result_to_payload(result: EventResult) -> dict:
    return {
        "published_at": result.published_at.isoformat(),
        "source": result.source,
        "source_url": result.source_url,
        "verdict_zh": result.verdict_zh,
        "summary_zh": result.summary_zh,
        "sector_impact_zh": result.sector_impact_zh,
        "portfolio_impact_zh": result.portfolio_impact_zh,
        "tone": result.tone,
        "metrics": [
            {
                "label_zh": metric.label_zh,
                "actual": metric.actual,
                "expected": metric.expected,
                "previous": metric.previous,
            }
            for metric in result.metrics
        ],
    }


def merge_cached_event_results(
    calendar: WeeklyEventCalendar,
    path: Path,
    as_of: datetime | None = None,
) -> WeeklyEventCalendar:
    """Restore results found by an earlier run so they do not disappear."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return calendar
    raw_results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(raw_results, dict):
        return calendar
    events: list[WeeklyEvent] = []
    for event in calendar.events:
        result = event.result or _event_result_from_payload(
            raw_results.get(event.event_id)
        )
        if result is not None and as_of is not None and result.published_at > as_of:
            result = None
        events.append(replace(event, result=result))
    return replace(calendar, events=events)


def _write_event_results_cache(
    calendar: WeeklyEventCalendar,
    path: Path,
    now: datetime,
) -> None:
    results = {
        event.event_id: _event_result_to_payload(event.result)
        for event in calendar.events
        if event.event_id and event.result is not None
    }
    payload = {
        "updated_at": now.astimezone(timezone.utc).isoformat(),
        "results": results,
    }
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_weekly_event_calendar(
    now: datetime,
    path: Path = WEEKLY_EVENTS_PATH,
) -> WeeklyEventCalendar:
    """Load source-verified events for the current or coming market week."""
    week_start, week_end = market_week_dates(now)
    empty = WeeklyEventCalendar(week_start=week_start, week_end=week_end)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"  Weekly event calendar unavailable: {exc}", file=sys.stderr)
        return empty

    if not isinstance(payload, dict):
        print("  Weekly event calendar unavailable: root must be an object", file=sys.stderr)
        return empty

    verified_value = payload.get("verified_at")
    verified_at = parse_ts(str(verified_value)) if verified_value else None
    portfolio_symbols = tuple(
        str(symbol).strip().upper()
        for symbol in payload.get("portfolio_symbols") or []
        if str(symbol).strip()
    )
    events: list[WeeklyEvent] = []
    for raw in payload.get("events") or []:
        try:
            starts_at = parse_ts(raw.get("starts_at"))
            if starts_at is None:
                raise ValueError("missing starts_at")
            event_date = starts_at.astimezone(NY_TZ).date()
            if not (week_start <= event_date <= week_end):
                continue
            source_url = str(raw.get("source_url") or "")
            if urlparse(source_url).scheme not in {"http", "https"}:
                raise ValueError("invalid source_url")
            impacts = tuple(
                PortfolioImpact(
                    symbols=tuple(
                        str(symbol).strip().upper()
                        for symbol in item.get("symbols") or []
                        if str(symbol).strip()
                    ),
                    sensitivity=str(item.get("sensitivity") or "未标注"),
                    summary_zh=str(item.get("summary_zh") or ""),
                )
                for item in raw.get("portfolio_impacts") or []
            )
            title_zh = str(raw.get("title_zh") or "未命名事件")
            event_id = str(raw.get("id") or "").strip()
            if not event_id:
                slug = re.sub(r"[^a-z0-9]+", "-", title_zh.lower()).strip("-")
                event_id = f"{event_date.isoformat()}-{slug or 'event'}"
            event_result = _event_result_from_payload(raw.get("result"))
            if event_result is not None and event_result.published_at > now:
                event_result = None
            event = WeeklyEvent(
                starts_at=starts_at,
                time_label_zh=str(raw.get("time_label_zh") or "时间待确认"),
                title_zh=title_zh,
                importance=str(raw.get("importance") or "中"),
                source=str(raw.get("source") or "来源未提供"),
                source_url=source_url,
                watch_zh=str(raw.get("watch_zh") or ""),
                bullish_if_zh=str(raw.get("bullish_if_zh") or ""),
                bearish_if_zh=str(raw.get("bearish_if_zh") or ""),
                bullish_sectors=tuple(
                    str(item) for item in raw.get("bullish_sectors") or []
                ),
                bearish_sectors=tuple(
                    str(item) for item in raw.get("bearish_sectors") or []
                ),
                portfolio_impacts=impacts,
                event_id=event_id,
                result_kind=str(raw.get("result_kind") or "").strip(),
                result_terms=tuple(
                    str(item).strip().lower()
                    for item in raw.get("result_terms") or []
                    if str(item).strip()
                ),
                result=event_result,
            )
            events.append(event)
        except (AttributeError, TypeError, ValueError) as exc:
            print(f"  Skipping invalid weekly event: {exc}", file=sys.stderr)

    events.sort(key=lambda event: event.starts_at)
    return WeeklyEventCalendar(
        week_start=week_start,
        week_end=week_end,
        events=events,
        verified_at=verified_at,
        portfolio_symbols=portfolio_symbols,
        portfolio_note_zh=str(payload.get("portfolio_note_zh") or ""),
    )


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


def _first_match(text: str, patterns: tuple[str, ...]) -> str:
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return match.group(1).replace(",", "")
    return ""


def _event_article_text(article: dict) -> str:
    return _clean_markup(
        " ".join(
            str(article.get(key) or "")
            for key in ("headline", "summary", "content")
        )
    )


def _metric_value(value: str, suffix: str = "") -> str:
    return f"{value}{suffix}" if value else ""


def _ism_result_from_article(event: WeeklyEvent, article: dict) -> EventResult | None:
    text = _event_article_text(article)
    flavor = "services" if event.result_kind == "ism_services" else "manufacturing"
    actual = _first_match(
        text,
        (
            rf"ISM\s+{flavor}\s+PMI[^.\n]{{0,100}}?(?:to|at|registered)\s+(\d{{2}}(?:\.\d+)?)",
            rf"{flavor}\s+PMI[^.\n]{{0,100}}?(?:to|at|registered)\s+(\d{{2}}(?:\.\d+)?)",
        ),
    )
    if not actual:
        return None
    previous = _first_match(
        text,
        (
            r"(?:from|previous (?:month|reading)(?: of)?|compared to [^,.]{0,35}?figure of)\s+(\d{2}(?:\.\d+)?)",
        ),
    )
    expected = _first_match(
        text,
        (
            r"(?:market |consensus )?(?:estimates?|expectations?|forecast)(?: of| at)?\s+(\d{2}(?:\.\d+)?)",
            r"(?:vs\.?|versus)\s+(\d{2}(?:\.\d+)?)\s+(?:estimate|consensus)",
        ),
    )
    actual_value = float(actual)
    expected_value = float(expected) if expected else None
    if expected_value is not None and actual_value >= expected_value + 0.3:
        verdict = "增长强于预期"
    elif expected_value is not None and actual_value <= expected_value - 0.3:
        verdict = "增长弱于预期"
    elif actual_value >= 50:
        verdict = "维持扩张"
    else:
        verdict = "处于收缩"
    tone = "negative" if actual_value < 50 else "mixed"
    sector = "服务业、金融和消费板块先看需求改善；高估值科技同时要防范收益率上行。"
    portfolio = "VOO受增长支撑；QQQM、SMH、MRVL和SPCX的净影响取决于强数据是否继续推高利率。"
    if flavor == "manufacturing":
        sector = "工业、材料和运输先看订单与生产；半导体还要结合价格分项和终端需求。"
        portfolio = "SMH与MRVL对订单周期最敏感；QQQM、SPCX和VOO主要通过增长与利率预期受影响。"
    published_at = parse_ts(article.get("created_at")) or event.starts_at
    return EventResult(
        published_at=published_at,
        source=str(article.get("source") or "新闻来源"),
        source_url=_safe_url(str(article.get("url") or event.source_url)),
        verdict_zh=verdict,
        summary_zh=(
            f"ISM {flavor.title()} PMI 公布为 {actual}"
            + (f"，高于预期 {expected}" if expected and actual_value > float(expected) else "")
            + (f"，低于预期 {expected}" if expected and actual_value < float(expected) else "")
            + (f"；前值 {previous}。" if previous else "。")
        ),
        sector_impact_zh=sector,
        portfolio_impact_zh=portfolio,
        tone=tone,
        metrics=(
            EventMetric(
                label_zh="ISM 服务业 PMI" if flavor == "services" else "ISM 制造业 PMI",
                actual=actual,
                expected=expected,
                previous=previous,
            ),
        ),
    )


def _employment_result_from_article(event: WeeklyEvent, article: dict) -> EventResult | None:
    text = _event_article_text(article)
    payroll = _first_match(
        text,
        (
            r"nonfarm payrolls?\s+(?:came in at|came at|contracted by|contracted|fell by|fell|declined by|declined|cut by|cut|lost|rose by|rose|increased by|increased|grew by|grew|added)\s+(-?[\d,]+)\s*[Kk]?",
            r"payrolls?\s+(?:came in at|came at|contracted by|contracted|fell by|fell|declined by|declined|cut by|cut|lost|rose by|rose|increased by|increased|grew by|grew|added)\s+(-?[\d,]+)\s*[Kk]?",
            r"(?:U\.?S\.?\s+)?employers?\s+(?:added|cut|shed|lost)\s+(-?[\d,]+)\s*[Kk]?",
            r"(?:U\.?S\.?\s+)?(?:economy|businesses?)\s+(?:added|cut|shed|lost)\s+(-?[\d,]+)\s*[Kk]?\s+jobs",
        ),
    )
    if not payroll:
        return None
    payroll_match = re.search(r"nonfarm payrolls?", text, flags=re.IGNORECASE)
    payroll_context = text[payroll_match.start():payroll_match.start() + 360] if payroll_match else text
    expected = _first_match(
        payroll_context,
        (
            r"(?:against|versus|vs\.?|compared (?:with|to))\s+(?:(?:a\s+)?(?:consensus\s+)?(?:estimate|forecast)(?:\s+of)?\s+)?(-?[\d,]+)\s*[Kk]?",
            r"(?:consensus|forecast|expected)\s+(?:was\s+|of\s+|at\s+|a gain of\s+)?(-?[\d,]+)\s*[Kk]?",
        ),
    )
    unemployment = _first_match(
        text,
        (
            r"unemployment(?: rate)?\s+(?:came in at|came at|was|held at|rose to|fell to|declined to|increased to)?\s*(\d+(?:\.\d+)?)%",
        ),
    )
    unemployment_context_match = re.search(
        r"unemployment(?: rate)?", text, flags=re.IGNORECASE
    )
    unemployment_context = (
        text[unemployment_context_match.start():unemployment_context_match.start() + 220]
        if unemployment_context_match else ""
    )
    unemployment_expected = _first_match(
        unemployment_context,
        (
            r"(?:versus|vs\.?|compared (?:with|to))\s+(?:the\s+)?(\d+(?:\.\d+)?)%",
            r"(?:expected|estimate|forecast|expectations?)(?:\s+at|\s+of|\s+for)?\s+(\d+(?:\.\d+)?)%",
        ),
    )
    wage = _first_match(
        text,
        (
            r"average hourly earnings\s+(?:came in at|rose by|rose|increased by|increased)\s+(\d+(?:\.\d+)?)%\s+(?:on the month|month-over-month|m/m)",
            r"average hourly\s+(?:came in at|rose by|rose|increased by|increased)\s+(\d+(?:\.\d+)?)%",
            r"average hourly earnings(?: growth)?(?:\s+(?:was|at))?\s+(\d+(?:\.\d+)?)%",
        ),
    )
    payroll_value = int(payroll)
    if payroll_value > 0 and re.search(
        r"(?:(?:nonfarm\s+)?payrolls?|employers?|economy|businesses?)[^.]{0,45}?(?:contracted|fell|declined|cut|shed|lost)",
        text,
        flags=re.IGNORECASE,
    ):
        payroll_value = -payroll_value
        payroll = str(payroll_value)
    if abs(payroll_value) >= 1000:
        payroll_value = round(payroll_value / 1000)
        payroll = str(payroll_value)
    expected_value = int(expected) if expected else None
    if expected_value is not None and abs(expected_value) >= 1000:
        expected_value = round(expected_value / 1000)
        expected = str(expected_value)
    unemployment_value = float(unemployment) if unemployment else None
    unemployment_expected_value = (
        float(unemployment_expected) if unemployment_expected else None
    )
    if expected_value is not None and payroll_value >= expected_value + 75:
        verdict = "就业偏热"
        tone = "negative"
        summary = "就业明显强于预期，利率上行风险增加。"
        sector = "金融和部分周期板块受增长支撑；高估值科技、REITs和小盘成长承受利率压力。"
        portfolio = "QQQM、SMH、MRVL和SPCX偏利空；VOO受增长与估值两股力量拉扯。"
    elif expected_value is not None and payroll_value <= expected_value - 75:
        verdict = "就业明显转弱"
        tone = "negative"
        if (
            unemployment_value is not None
            and unemployment_expected_value is not None
            and unemployment_value > unemployment_expected_value
        ):
            summary = "就业大幅低于预期且失业率偏高，降息预期上升，但衰退风险也在增加。"
        else:
            summary = "就业大幅低于预期，市场会提高降息押注，同时重新评估增长下行风险。"
        sector = "长久期资产可能先受益于收益率回落；金融、消费和小盘周期面临盈利压力。"
        portfolio = "QQQM和SMH可能先获利率支撑；MRVL、SPCX和VOO仍需防范增长下修。"
    elif expected_value is not None and payroll_value <= expected_value - 40:
        verdict = "就业温和降温"
        tone = "positive"
        summary = "就业低于预期但尚未显示失速，市场更可能交易降息预期。"
        sector = "长久期科技与REITs偏受益；周期和金融板块表现取决于失业率。"
        portfolio = "QQQM、SMH和MRVL偏利好；SPCX波动可能放大，VOO影响中性偏正面。"
    else:
        verdict = "就业大致符合预期"
        tone = "neutral"
        summary = "就业结果接近预期，市场影响更取决于失业率、工资和历史修正。"
        sector = "板块影响偏中性，继续观察工资、收益率和盈利预期。"
        portfolio = "SMH、MRVL、QQQM、SPCX和VOO暂无单一方向信号。"
    metrics = [
        EventMetric(
            label_zh="非农就业",
            actual=_metric_value(payroll, "K"),
            expected=_metric_value(expected, "K"),
        )
    ]
    if unemployment:
        metrics.append(
            EventMetric(
                label_zh="失业率",
                actual=_metric_value(unemployment, "%"),
                expected=_metric_value(unemployment_expected, "%"),
            )
        )
    if wage:
        metrics.append(EventMetric(label_zh="平均时薪月率", actual=_metric_value(wage, "%")))
    published_at = parse_ts(article.get("created_at")) or event.starts_at
    return EventResult(
        published_at=published_at,
        source=str(article.get("source") or "新闻来源"),
        source_url=_safe_url(str(article.get("url") or event.source_url)),
        verdict_zh=verdict,
        summary_zh=(
            f"非农就业 {payroll}K"
            + (f"，预期 {expected}K" if expected else "")
            + (f"；失业率 {unemployment}%" if unemployment else "")
            + (f"，预期 {unemployment_expected}%" if unemployment_expected else "")
            + f"。{summary}"
        ),
        sector_impact_zh=sector,
        portfolio_impact_zh=portfolio,
        tone=tone,
        metrics=tuple(metrics),
    )


def _event_result_from_article(event: WeeklyEvent, article: dict) -> EventResult | None:
    if event.result_kind in {"ism_services", "ism_manufacturing"}:
        return _ism_result_from_article(event, article)
    if event.result_kind == "employment":
        return _employment_result_from_article(event, article)
    return None


def update_weekly_event_results(
    calendar: WeeklyEventCalendar,
    articles: list[dict],
    now: datetime,
) -> WeeklyEventCalendar:
    """Attach newly published event results found in the current news pull."""
    refreshed: list[WeeklyEvent] = []
    for event in calendar.events:
        if event.result is not None or not event.result_kind or not event.result_terms:
            refreshed.append(event)
            continue
        if now < event.starts_at + timedelta(minutes=EVENT_RESULT_GRACE_MINUTES):
            refreshed.append(event)
            continue
        window_start = event.starts_at - timedelta(minutes=15)
        window_end = event.starts_at + timedelta(hours=EVENT_RESULT_LOOKBACK_HOURS)
        candidates: list[tuple[int, datetime, dict]] = []
        for article in articles:
            created_at = parse_ts(article.get("created_at"))
            if created_at is None or not (window_start <= created_at <= min(now, window_end)):
                continue
            headline = _clean_markup(str(article.get("headline") or "")).lower()
            summary = _clean_markup(str(article.get("summary") or "")).lower()
            matching_terms = [
                term for term in event.result_terms if term in headline or term in summary
            ]
            if not matching_terms:
                continue
            score = sum(5 if term in headline else 2 for term in matching_terms)
            combined = f"{headline} {summary}"
            if "consensus" in combined or "estimate" in combined or "expected" in combined:
                score += 2
            candidates.append((score, created_at, article))
        candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
        result = None
        for _score, _created_at, article in candidates:
            result = _event_result_from_article(event, article)
            if result is not None:
                print(
                    f"  Event result found: {event.title_zh} — "
                    f"{result.verdict_zh} ({result.source})"
                )
                break
        refreshed.append(replace(event, result=result) if result else event)
    return replace(calendar, events=refreshed)


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


def _premarket_session_date(now: datetime) -> date | None:
    local = now.astimezone(NY_TZ)
    if local.weekday() >= 5:
        return None
    if PREMARKET_START_ET <= local.time() < REGULAR_MARKET_OPEN_ET:
        return local.date()
    return None


def _fetch_intraday_bar_pages(
    api_key: str,
    api_secret: str,
    start: datetime,
    end: datetime,
    feed: str,
    client=requests,
) -> dict[str, list[dict]]:
    params: dict[str, str | int] = {
        "symbols": ",".join(PREMARKET_SYMBOLS),
        "timeframe": "1Min",
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


def _classify_premarket(moves: dict[str, PremarketMove]) -> str:
    broad_changes = [
        moves[symbol].change_pct
        for symbol in ("SPY", "QQQ", "IWM")
        if symbol in moves
    ]
    if not broad_changes:
        return "盘前数据有限"
    if max(broad_changes) >= 0.35 and min(broad_changes) <= -0.35:
        return "盘前明显分化"
    center = median(broad_changes)
    if center >= 0.35:
        return "盘前偏强"
    if center <= -0.35:
        return "盘前偏弱"
    return "盘前波动有限"


def build_premarket_pulse(
    raw_bars: dict[str, list[dict]],
    market_pulse: MarketPulse,
    now: datetime,
    feed: str,
) -> PremarketPulse:
    session_date = _premarket_session_date(now)
    if session_date is None:
        return PremarketPulse(session_date=None)

    moves: dict[str, PremarketMove] = {}
    latest_timestamp: datetime | None = None
    for symbol in PREMARKET_SYMBOLS:
        prior_move = market_pulse.moves.get(symbol)
        if not prior_move or prior_move.close <= 0:
            continue
        rows: list[tuple[datetime, dict]] = []
        for bar in raw_bars.get(symbol, []):
            timestamp = parse_ts(bar.get("t"))
            if not timestamp or bar.get("c") is None:
                continue
            local_timestamp = timestamp.astimezone(NY_TZ)
            if local_timestamp.date() != session_date:
                continue
            if not PREMARKET_START_ET <= local_timestamp.time() < REGULAR_MARKET_OPEN_ET:
                continue
            rows.append((timestamp, bar))
        rows.sort(key=lambda pair: pair[0])
        if not rows:
            continue

        as_of, latest = rows[-1]
        price = float(latest["c"])
        previous_close = prior_move.close
        move = PremarketMove(
            symbol=symbol,
            label=MARKET_SYMBOLS.get(symbol, symbol),
            price=price,
            previous_close=previous_close,
            change_pct=(price / previous_close - 1) * 100,
            volume=int(sum(float(bar.get("v") or 0) for _, bar in rows)),
            high=max(float(bar.get("h") or bar["c"]) for _, bar in rows),
            low=min(float(bar.get("l") or bar["c"]) for _, bar in rows),
            as_of=as_of,
        )
        moves[symbol] = move
        if latest_timestamp is None or as_of > latest_timestamp:
            latest_timestamp = as_of

    feed_note = (
        f"延迟 SIP 全市场 1 分钟成交聚合（至少延迟 {MARKET_DATA_DELAY_MINUTES} 分钟）"
        if feed == "sip"
        else f"IEX 单一交易所 1 分钟成交聚合（至少延迟 {MARKET_DATA_DELAY_MINUTES} 分钟）"
    )
    return PremarketPulse(
        session_date=session_date,
        moves=moves,
        status=_classify_premarket(moves),
        feed=feed,
        as_of=latest_timestamp,
        note=feed_note,
    )


def fetch_premarket_pulse(
    api_key: str,
    api_secret: str,
    market_pulse: MarketPulse,
    now: datetime,
    client=requests,
) -> PremarketPulse:
    session_date = _premarket_session_date(now)
    if session_date is None:
        return PremarketPulse(session_date=None)

    start_et = datetime.combine(session_date, PREMARKET_START_ET, tzinfo=NY_TZ)
    end = now - timedelta(minutes=MARKET_DATA_DELAY_MINUTES)
    if end <= start_et.astimezone(timezone.utc):
        return PremarketPulse(
            session_date=session_date,
            note=f"盘前刚开始，等待至少延迟 {MARKET_DATA_DELAY_MINUTES} 分钟的数据。",
        )

    errors: list[str] = []
    for feed in ("sip", "iex"):
        try:
            raw_bars = _fetch_intraday_bar_pages(
                api_key,
                api_secret,
                start_et.astimezone(timezone.utc),
                end,
                feed,
                client=client,
            )
            premarket = build_premarket_pulse(raw_bars, market_pulse, now, feed)
            if premarket.available:
                return premarket
            errors.append(f"{feed}: 没有当日盘前成交")
        except Exception as exc:
            errors.append(f"{feed}: {exc}")
            print(f"  Premarket data fetch failed for {feed}: {exc}", file=sys.stderr)
    return PremarketPulse(
        session_date=session_date,
        note="盘前行情暂不可用。" + (" " + " | ".join(errors) if errors else ""),
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


def current_section_news(articles: list[dict], now: datetime) -> list[dict]:
    """Keep weekend current-content sections genuinely Saturday/Sunday fresh.

    Friday market drivers remain available to explain the last completed close,
    but weekend sector, focus-list, and world sections must not be filled by a
    higher-scoring Friday recap merely because US cash trading is closed.
    """
    local = now.astimezone(NY_TZ)
    if local.weekday() < 5:
        return list(articles)
    saturday = local.date() - timedelta(days=local.weekday() - 5)
    selected: list[dict] = []
    for article in articles:
        article_date = _article_date_et(article)
        if article_date is not None and saturday <= article_date <= local.date():
            selected.append(article)
    return selected


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


def _article_display_headline(article: dict) -> str:
    return _clean_markup(
        article.get("_zh_headline") or article.get("headline") or "未命名报道"
    )


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
    display_headline = _article_display_headline(article)
    original_html = (
        f'<p class="original-headline">原文：{_html_escape(headline)}</p>'
        if zh_headline and headline and zh_headline.strip() != headline.strip() else ""
    )
    detail_summary = zh_summary or summary
    detail_html = (
        '<details class="story-details">'
        '<summary>查看摘要与原文</summary>'
        f'{original_html}'
        f'<p class="story-summary">{_html_escape(detail_summary)}</p>'
        '</details>'
        if original_html or detail_summary else ""
    )
    return (
        '<article class="story">\n'
        f'  <div class="story-number">{index:02d}</div>\n'
        '  <div class="story-content">\n'
        f'    <div class="meta">{_html_escape(source)} · '
        f'{_html_escape(_format_article_time(article))}{_html_escape(symbol_text)}'
        f'{badge_html}</div>\n'
        f'    <h3><a href="{_html_escape(url)}" target="_blank" rel="noopener noreferrer">'
        f'{_html_escape(display_headline)}</a></h3>\n'
        f'    {detail_html}\n'
        '  </div>\n'
        '</article>'
    )


def _format_pct(value: float) -> str:
    return f"{value:+.2f}%"


def _format_volume(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}K"
    return str(value)


def _premarket_section_html(premarket: PremarketPulse | None) -> str:
    if premarket is None or not premarket.active:
        return ""
    session = premarket.session_date.strftime("%A, %B %-d, %Y")
    if not premarket.available:
        return (
            '<section class="premarket"><div class="section-kicker">PREMARKET · DELAYED</div>'
            f'<div class="session-date">盘前行情 · {_html_escape(session)}</div>'
            '<h2>盘前行情暂不可用</h2>'
            f'<p class="pulse-summary">{_html_escape(premarket.note)}</p></section>'
        )

    cards: list[str] = []
    for symbol in PREMARKET_SYMBOLS:
        move = premarket.moves.get(symbol)
        if move is None:
            cards.append(
                '<div class="premarket-card unavailable">'
                f'<div class="move-label">{_html_escape(symbol)} · '
                f'{_html_escape(MARKET_SYMBOLS.get(symbol, symbol))}</div>'
                '<div class="premarket-empty">暂无当日盘前成交</div>'
                '</div>'
            )
            continue
        css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
        move_as_of = move.as_of.astimezone(NY_TZ).strftime("%-I:%M %p ET")
        cards.append(
            '<div class="premarket-card">'
            f'<div class="move-label">{_html_escape(symbol)} · {_html_escape(move.label)}</div>'
            '<div class="premarket-primary">'
            f'<div class="premarket-price">${move.price:,.2f}</div>'
            f'<div class="premarket-change {css_class}">{_format_pct(move.change_pct)}</div>'
            '</div>'
            '<details class="mini-details">'
            '<summary>成交细节</summary>'
            f'<div class="premarket-meta">相对昨收 · 量 {_format_volume(move.volume)} · '
            f'区间 ${move.low:,.2f}–${move.high:,.2f} · '
            f'截至 {_html_escape(move_as_of)}</div>'
            '</details>'
            '</div>'
        )
    as_of = (
        premarket.as_of.astimezone(NY_TZ).strftime("%-I:%M %p ET")
        if premarket.as_of else "时间未知"
    )
    return (
        '<section class="premarket">'
        '<div class="section-kicker">PREMARKET · DELAYED</div>'
        f'<div class="session-date">盘前行情 · {_html_escape(session)} · 最新成交截至 {_html_escape(as_of)}</div>'
        f'<h2>{_html_escape(premarket.status)}</h2>'
        f'<div class="premarket-grid">{"".join(cards)}</div>'
        '<details class="section-details">'
        '<summary>数据说明</summary>'
        f'<p class="data-note">{_html_escape(premarket.note)}。涨跌均相对最近完整交易日收盘；'
        '成交量为 4:00 AM ET 起盘前累计，不可与完整常规交易时段成交量直接比较。</p>'
        '</details>'
        '</section>'
    )


def _event_result_html(result: EventResult) -> str:
    tone = result.tone if result.tone in {"positive", "negative", "mixed", "neutral"} else "neutral"
    metrics = "".join(
        '<div class="result-metric">'
        f'<span>{_html_escape(metric.label_zh)}</span>'
        f'<strong>{_html_escape(metric.actual)}</strong>'
        '<small>'
        + (
            f'预期 {_html_escape(metric.expected)}'
            if metric.expected else "预期未提供"
        )
        + (
            f' · 前值 {_html_escape(metric.previous)}'
            if metric.previous else ""
        )
        + '</small></div>'
        for metric in result.metrics
    )
    published = result.published_at.astimezone(NY_TZ).strftime("%-m/%-d %-I:%M %p ET")
    return (
        f'<div class="event-result result-{tone}">'
        '<div class="result-kicker">已公布结果</div>'
        f'<div class="result-verdict">{_html_escape(result.verdict_zh)}</div>'
        f'<p class="result-summary">{_html_escape(result.summary_zh)}</p>'
        f'<div class="result-metrics">{metrics}</div>'
        f'<p class="result-impact"><strong>板块：</strong>{_html_escape(result.sector_impact_zh)}</p>'
        f'<p class="result-impact"><strong>你的仓位：</strong>{_html_escape(result.portfolio_impact_zh)}</p>'
        '<div class="event-source">结果来源：'
        f'<a href="{_html_escape(_safe_url(result.source_url))}" target="_blank" '
        f'rel="noopener noreferrer">{_html_escape(result.source)}</a>'
        f' · {_html_escape(published)}</div>'
        '</div>'
    )


def _weekly_events_html(
    calendar: WeeklyEventCalendar,
    now: datetime,
) -> str:
    week_label = (
        f"{calendar.week_start.strftime('%-m/%-d')}–"
        f"{calendar.week_end.strftime('%-m/%-d')} ET"
    )
    if not calendar.events:
        return (
            '<section class="weekly-events">'
            '<div class="section-kicker">WEEK AHEAD · VERIFIED</div>'
            f'<div class="session-date">{_html_escape(week_label)}</div>'
            '<h2>本周关键事件</h2>'
            '<p class="empty">当前周没有已核实并录入的关键事件；不会用未确认日期占位。</p>'
            '</section>'
        )

    future_times = [
        event.starts_at for event in calendar.events if event.starts_at > now
    ]
    next_time = min(future_times) if future_times else None
    cards: list[str] = []
    for event in calendar.events:
        if event.result is not None:
            status_text, status_class = f"已公布 · {event.result.verdict_zh}", "released"
        elif event.starts_at + timedelta(minutes=EVENT_RESULT_GRACE_MINUTES) <= now:
            status_text, status_class = "等待结果", "pending-result"
        elif event.starts_at <= now:
            status_text, status_class = "公布中", "pending-result"
        elif next_time is not None and event.starts_at == next_time:
            status_text, status_class = "下一个", "next"
        else:
            status_text, status_class = "待公布", "upcoming"

        bullish_tags = "".join(
            f'<span class="impact-tag positive">{_html_escape(item)}</span>'
            for item in event.bullish_sectors
        )
        bearish_tags = "".join(
            f'<span class="impact-tag negative">{_html_escape(item)}</span>'
            for item in event.bearish_sectors
        )
        portfolio_rows = "".join(
            '<div class="portfolio-row">'
            '<div class="portfolio-symbols">'
            f'{_html_escape(" / ".join(impact.symbols) or "未指定")}'
            f'<span>{_html_escape(impact.sensitivity)}敏感</span>'
            '</div>'
            f'<p>{_html_escape(impact.summary_zh)}</p>'
            '</div>'
            for impact in event.portfolio_impacts
        )
        result_html = _event_result_html(event.result) if event.result else ""
        scenario_html = (
            '<div class="scenario-grid">'
            '<div class="scenario positive">'
            '<div class="scenario-label">偏利好情景</div>'
            f'<p>{_html_escape(event.bullish_if_zh)}</p>'
            f'<div class="impact-tags">{bullish_tags}</div>'
            '</div>'
            '<div class="scenario negative">'
            '<div class="scenario-label">偏利空情景</div>'
            f'<p>{_html_escape(event.bearish_if_zh)}</p>'
            f'<div class="impact-tags">{bearish_tags}</div>'
            '</div>'
            '</div>'
            '<div class="portfolio-impact">'
            f'<div class="portfolio-title">{"公布前敏感度预案" if event.result else "对当前监控仓位的影响"}</div>'
            f'{portfolio_rows}'
            '</div>'
        )
        if event.result:
            scenario_html = (
                '<details class="pre-event-scenarios">'
                '<summary>查看公布前情景预案</summary>'
                f'{scenario_html}'
                '</details>'
            )
        cards.append(
            f'<details class="event-card event-{status_class}">'
            '<summary class="event-summary disclosure-summary">'
            '<div class="event-summary-copy">'
            '<div class="event-topline">'
            f'<span class="event-time">{_html_escape(event.time_label_zh)}</span>'
            f'<span class="event-status">{_html_escape(status_text)} · '
            f'{_html_escape(event.importance)}重要度</span>'
            '</div>'
            f'<h3>{_html_escape(event.title_zh)}</h3>'
            f'<p class="event-watch">{_html_escape(event.result.summary_zh if event.result else event.watch_zh)}</p>'
            '</div>'
            '</summary>'
            '<div class="event-expanded">'
            f'{result_html}'
            f'{scenario_html}'
            f'<div class="event-source">官方时间来源：'
            f'<a href="{_html_escape(_safe_url(event.source_url))}" target="_blank" '
            f'rel="noopener noreferrer">{_html_escape(event.source)}</a></div>'
            '</div>'
            '</details>'
        )

    verified = (
        calendar.verified_at.astimezone(NY_TZ).strftime("%-m/%-d %-I:%M %p ET")
        if calendar.verified_at else "时间未记录"
    )
    portfolio = " / ".join(calendar.portfolio_symbols) or "未配置"
    return (
        '<section class="weekly-events">'
        '<div class="section-kicker">WEEK AHEAD · VERIFIED</div>'
        f'<div class="session-date">{_html_escape(week_label)} · 日历核实于 '
        f'{_html_escape(verified)}</div>'
        '<h2>本周关键事件</h2>'
        '<p class="section-note">公布后直接显示实际结果；点击事件查看板块和仓位影响。</p>'
        f'{"".join(cards)}'
        '<details class="section-details">'
        '<summary>仓位与日历说明</summary>'
        f'<p class="data-note">当前监控清单：{_html_escape(portfolio)}。'
        f'{_html_escape(calendar.portfolio_note_zh)} 这里只评估方向和敏感度，不构成交易建议。</p>'
        '</details>'
        '</section>'
    )


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
        '<details class="inline-details">'
        '<summary>查看判断依据</summary>'
        f'<ul>{evidence}</ul>'
        '</details>'
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
        '<details class="section-details market-details">'
        '<summary>新闻归因与数据说明</summary>'
        '<div class="explanation">'
        f'<div class="confidence">{_html_escape(assessment.confidence)}置信度</div>'
        f'<p>{_html_escape(assessment.summary_zh)}</p>'
        '</div>'
        f'<p class="data-note">{_html_escape(pulse.note)}。ETF 用作指数代理；'
        '新闻归因基于同日来源和方向一致性，不是可证明的唯一因果。</p>'
        '</details>'
        '</section>'
    )


def _sector_groups_html(
    groups: list[SectorGroup],
    pulse: MarketPulse,
    premarket: PremarketPulse | None = None,
) -> str:
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
        premarket_move = premarket.moves.get(group.proxy_symbol or "") if premarket else None
        move = pulse.moves.get(group.proxy_symbol or "")
        if premarket_move:
            css_class = "up" if premarket_move.change_pct > 0 else "down" if premarket_move.change_pct < 0 else "flat"
            proxy_html = (
                f'<span class="sector-move {css_class}">盘前 {premarket_move.symbol} '
                f'{_format_pct(premarket_move.change_pct)}</span>'
            )
        elif move:
            css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
            proxy_html = (
                f'<span class="sector-move {css_class}">昨收 {move.symbol} '
                f'{_format_pct(move.change_pct)}</span>'
            )
        stories: list[str] = []
        for article in group.articles:
            stories.append(_article_html(article, story_number))
            story_number += 1
        teaser = _article_display_headline(group.articles[0]) if group.articles else ""
        blocks.append(
            '<details class="sector-block">'
            '<summary class="group-summary disclosure-summary">'
            '<div class="group-summary-copy">'
            f'<div class="sector-heading"><h3>{_html_escape(group.label_zh)}</h3>'
            f'{proxy_html}</div>'
            f'<p class="group-teaser">{_html_escape(teaser)}</p>'
            '</div>'
            '</summary>'
            f'<div class="group-body">{"".join(stories)}</div>'
            '</details>'
        )
    return (
        '<section class="sector-news"><div class="section-kicker">SECTOR MAP</div>'
        '<h2>板块核心新闻</h2>'
        '<p class="section-note">点击板块查看新闻和摘要。</p>'
        f'{"".join(blocks)}</section>'
    )


def _special_watch_html(
    spec: SpecialWatchSpec,
    pulse: MarketPulse,
    articles: list[dict],
    premarket: PremarketPulse | None = None,
) -> str:
    premarket_move = premarket.moves.get(spec.symbol) if premarket else None
    move = pulse.moves.get(spec.symbol)
    if premarket_move:
        css_class = "up" if premarket_move.change_pct > 0 else "down" if premarket_move.change_pct < 0 else "flat"
        move_html = (
            '<div class="special-quote">'
            '<span class="special-session">盘前</span>'
            f'<span class="special-price">{premarket_move.price:,.2f}</span>'
            f'<span class="special-change {css_class}">{_format_pct(premarket_move.change_pct)}</span>'
            '</div>'
        )
    elif move:
        css_class = "up" if move.change_pct > 0 else "down" if move.change_pct < 0 else "flat"
        move_html = (
            '<div class="special-quote">'
            '<span class="special-session">昨收</span>'
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
    teaser = (
        _article_display_headline(articles[0])
        if articles else "最近 36 小时无达到门槛的专项新闻"
    )
    return (
        '<details class="special-block">'
        '<summary class="special-summary disclosure-summary">'
        '<div class="special-summary-copy">'
        f'<div class="special-heading"><h3>{_html_escape(spec.label_zh)}</h3>{move_html}</div>'
        f'<p class="group-teaser">{_html_escape(teaser)}</p>'
        '</div>'
        '</summary>'
        f'<div class="special-body">{note}{stories}</div>'
        '</details>'
    )


def _special_sections_html(
    pulse: MarketPulse,
    special_news: dict[str, list[dict]],
    premarket: PremarketPulse | None = None,
) -> str:
    blocks = "".join(
        _special_watch_html(spec, pulse, special_news.get(symbol, []), premarket)
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
        f'<h2>{_html_escape(title)}</h2>'
        '<p class="section-note">点击标题打开来源，点击“查看摘要与原文”展开内容。</p>'
        f'{items}</section>'
    )


HTML_TEMPLATE = Template("""<!DOCTYPE html>
<html lang="zh-CN" data-generated-at="$generated_iso">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#ffffff">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Market Brief">
<title>Market Brief — $date_short</title>
<link rel="manifest" href="manifest.webmanifest">
<link rel="icon" type="image/svg+xml" href="icon.svg">
<link rel="apple-touch-icon" href="icon.svg">
<style>
  :root {
    --bg: #ffffff; --surface: #ffffff; --surface-2: #f6f6f3;
    --ink: #24231f; --muted: #6b6a63; --line: #e4e3dc;
    --green: #15724c; --red: #b43f45; --amber: #9a5b2b;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; background: var(--bg); color: var(--ink); }
  body {
    font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    line-height: 1.52; padding: env(safe-area-inset-top) 0 env(safe-area-inset-bottom);
  }
  .wrap { width: min(900px, 100%); margin: 0 auto; padding: 0 24px; }
  header { padding: 30px 0 20px; border-bottom: 1px solid var(--line); }
  .eyebrow, .section-kicker {
    color: var(--amber); font-size: 11px; font-weight: 750; letter-spacing: .16em;
  }
  h1 { margin: 5px 0 0; font-size: clamp(30px, 6vw, 46px); line-height: 1.05; letter-spacing: -.04em; }
  .date { color: var(--muted); margin-top: 8px; font-size: 13px; }
  .refresh-status { display: flex; align-items: center; gap: 7px; color: var(--muted); margin-top: 8px; font-size: 12px; }
  .refresh-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 0 3px rgba(21, 114, 76, .09); }
  .refresh-status.delayed { color: var(--amber); }
  .refresh-status.delayed .refresh-dot { background: var(--amber); box-shadow: 0 0 0 3px rgba(154, 91, 43, .10); }
  .refresh-status.stale { color: var(--red); }
  .refresh-status.stale .refresh-dot { background: var(--red); box-shadow: 0 0 0 3px rgba(180, 63, 69, .10); }
  section { padding: 27px 0; border-bottom: 1px solid var(--line); }
  h2 { margin: 5px 0 14px; font-size: 23px; letter-spacing: -.02em; }
  summary { list-style: none; cursor: pointer; }
  summary::-webkit-details-marker { display: none; }
  summary:focus-visible { outline: 2px solid var(--amber); outline-offset: 4px; border-radius: 4px; }
  .disclosure-summary { display: grid; grid-template-columns: minmax(0, 1fr) 24px; gap: 12px; align-items: center; }
  .disclosure-summary::after { content: "+"; color: var(--muted); font-size: 22px; font-weight: 350; text-align: right; }
  details[open] > .disclosure-summary::after { content: "−"; }
  .session-date, .data-note, .confidence, .meta, .move-close, .sector-line, .empty {
    color: var(--muted); font-size: 12px;
  }
  .moves { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
  .move { padding: 13px; border: 1px solid var(--line); background: var(--surface); border-radius: 8px; }
  .move-label { color: var(--muted); font-size: 12px; }
  .move-value { margin: 3px 0 1px; font-size: 22px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .premarket-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; }
  .premarket-card { padding: 14px; border: 1px solid var(--line); background: var(--surface); border-radius: 8px; }
  .premarket-card.unavailable { display: flex; min-height: 92px; flex-direction: column; justify-content: space-between; }
  .premarket-primary { display: flex; align-items: baseline; justify-content: space-between; gap: 8px; }
  .premarket-price { margin-top: 4px; font-size: 21px; font-weight: 780; font-variant-numeric: tabular-nums; }
  .premarket-change { margin-top: 1px; font-size: 14px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .premarket-meta, .premarket-empty { margin-top: 7px; color: var(--muted); font-size: 11px; }
  .mini-details, .section-details, .inline-details, .story-details { margin-top: 8px; }
  .mini-details summary, .section-details summary, .inline-details summary, .story-details summary {
    width: fit-content; color: var(--muted); font-size: 11px; text-decoration: underline; text-decoration-color: var(--line); text-underline-offset: 3px;
  }
  .mini-details summary:hover, .section-details summary:hover, .inline-details summary:hover, .story-details summary:hover { color: var(--ink); }
  .section-details { margin-top: 13px; }
  .section-details[open] { padding: 12px 14px; background: var(--surface-2); border-radius: 7px; }
  .up { color: var(--green); } .down { color: var(--red); } .flat { color: var(--ink); }
  .sector-line { margin: 13px 0 0; }
  .explanation { margin-top: 10px; padding: 12px 14px; background: var(--surface); border-left: 2px solid var(--amber); }
  .explanation p { margin: 7px 0 0; font-size: 16px; }
  .overview { margin-top: 17px; padding: 16px; background: var(--surface-2); border-left: 3px solid var(--line); }
  .overview-kicker { color: var(--muted); font-size: 11px; letter-spacing: .13em; }
  .overview-label { margin-top: 3px; font-size: 22px; font-weight: 780; letter-spacing: -.02em; }
  .overview p { margin: 6px 0 0; }
  .overview ul { margin: 9px 0 0; padding-left: 19px; color: var(--muted); font-size: 12px; }
  .tone-positive, .tone-cautious-positive { border-left-color: var(--green); }
  .tone-negative, .tone-cautious-negative { border-left-color: var(--red); }
  .tone-neutral, .tone-unavailable { border-left-color: var(--amber); }
  .event-card { margin-top: 9px; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .event-card.event-next { border-left: 3px solid var(--amber); }
  .event-card.event-released { border-left: 3px solid var(--green); }
  .event-card.event-pending-result { border-left: 3px solid var(--amber); }
  .event-summary { padding: 13px 15px; }
  .event-summary-copy { min-width: 0; }
  .event-topline { display: flex; align-items: center; gap: 8px; }
  .event-time { color: var(--amber); font-size: 11px; font-weight: 780; }
  .event-status { color: var(--muted); font-size: 9px; letter-spacing: .06em; text-transform: uppercase; }
  .event-card h3 { margin: 4px 0 3px; font-size: 18px; line-height: 1.3; }
  .event-watch { display: -webkit-box; margin: 0; overflow: hidden; color: var(--muted); font-size: 12px; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
  .event-expanded { padding: 14px 15px 15px; border-top: 1px solid var(--line); background: var(--surface-2); }
  .event-result { margin-bottom: 11px; padding: 13px; border: 1px solid var(--line); border-left: 3px solid var(--amber); border-radius: 7px; background: var(--surface); }
  .event-result.result-positive { border-left-color: var(--green); }
  .event-result.result-negative { border-left-color: var(--red); }
  .result-kicker { color: var(--muted); font-size: 9px; font-weight: 780; letter-spacing: .12em; }
  .result-verdict { margin-top: 2px; font-size: 17px; font-weight: 780; }
  .result-summary { margin: 5px 0 0; font-size: 12px; }
  .result-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 7px; margin-top: 10px; }
  .result-metric { display: grid; gap: 1px; padding: 9px; border-radius: 6px; background: var(--surface-2); }
  .result-metric span, .result-metric small { color: var(--muted); font-size: 9px; }
  .result-metric strong { font-size: 17px; font-variant-numeric: tabular-nums; }
  .result-impact { margin: 9px 0 0; color: var(--muted); font-size: 12px; }
  .result-impact strong { color: var(--ink); }
  .pre-event-scenarios { margin-top: 10px; }
  .pre-event-scenarios > summary { width: fit-content; color: var(--muted); font-size: 11px; text-decoration: underline; text-underline-offset: 3px; }
  .pre-event-scenarios[open] > summary { margin-bottom: 10px; }
  .scenario-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .scenario { padding: 12px; border-radius: 7px; border: 1px solid var(--line); background: var(--surface); }
  .scenario.positive { border-left: 2px solid var(--green); }
  .scenario.negative { border-left: 2px solid var(--red); }
  .scenario-label { font-size: 12px; font-weight: 780; }
  .scenario.positive .scenario-label { color: var(--green); }
  .scenario.negative .scenario-label { color: var(--red); }
  .scenario p { margin: 6px 0 9px; color: var(--ink); font-size: 12px; }
  .impact-tags { display: flex; flex-wrap: wrap; gap: 5px; }
  .impact-tag { padding: 2px 6px; border-radius: 999px; font-size: 9px; border: 1px solid var(--line); background: var(--surface); }
  .impact-tag.positive { color: var(--green); }
  .impact-tag.negative { color: var(--red); }
  .portfolio-impact { margin-top: 10px; padding: 12px; background: var(--surface); border: 1px solid var(--line); border-radius: 7px; }
  .portfolio-title { margin-bottom: 7px; color: var(--amber); font-size: 11px; font-weight: 780; letter-spacing: .08em; }
  .portfolio-row { display: grid; grid-template-columns: minmax(130px, .38fr) 1fr; gap: 12px; padding: 8px 0; border-top: 1px solid var(--line); }
  .portfolio-row:first-of-type { border-top: 0; }
  .portfolio-symbols { font-size: 12px; font-weight: 780; }
  .portfolio-symbols span { display: block; color: var(--muted); font-size: 10px; font-weight: 500; }
  .portfolio-row p { margin: 0; color: var(--muted); font-size: 12px; }
  .event-source { margin-top: 11px; color: var(--muted); font-size: 10px; }
  .event-source a { color: inherit; text-underline-offset: 2px; }
  .data-note { margin: 14px 0 0; }
  .section-note, .special-note { color: var(--muted); font-size: 12px; }
  .sector-block { margin-top: 9px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); overflow: hidden; }
  .group-summary, .special-summary { padding: 13px 15px; }
  .group-summary-copy, .special-summary-copy { min-width: 0; }
  .sector-heading, .special-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
  .sector-heading h3, .special-heading h3 { margin: 0; font-size: 18px; }
  .group-teaser { margin: 4px 0 0; overflow: hidden; color: var(--muted); font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
  .group-body, .special-body { padding: 0 15px 3px; border-top: 1px solid var(--line); background: var(--surface-2); }
  .sector-move { font-size: 13px; font-weight: 750; font-variant-numeric: tabular-nums; }
  .story { display: grid; grid-template-columns: 30px 1fr; gap: 8px; padding: 14px 0; border-top: 1px solid var(--line); }
  .story-content { min-width: 0; }
  .sector-block .story:first-of-type { border-top: 0; }
  .story-number { color: #b4b3a8; font-size: 11px; padding-top: 2px; }
  .story h3 { margin: 4px 0 2px; font-size: 16px; line-height: 1.35; }
  .story h3 a { color: var(--ink); text-decoration: none; }
  .story h3 a:hover { color: var(--green); }
  .story-badge { margin-left: 8px; color: var(--amber); font-size: 10px; letter-spacing: .08em; }
  .story-details[open] { margin-top: 8px; }
  .original-headline { margin: 8px 0 0; color: var(--muted); font-size: 11px; }
  .story-summary { margin: 6px 0 0; color: var(--muted); font-size: 12px; }
  .special-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
  .special-block { min-width: 0; background: var(--surface); border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
  .special-quote { display: flex; align-items: baseline; gap: 8px; white-space: nowrap; }
  .special-price { font-size: 19px; font-weight: 750; }
  .special-change { font-size: 13px; font-weight: 750; }
  .special-session { color: var(--muted); font-size: 10px; letter-spacing: .08em; }
  .special-quote.unavailable { color: var(--muted); font-size: 12px; }
  .special-body .special-note { margin: 12px 0 0; }
  .special-block .story { grid-template-columns: 24px 1fr; }
  .special-block .story h3 { font-size: 16px; }
  footer { padding: 24px 0 36px; color: var(--muted); font-size: 10px; }
  @media (max-width: 680px) {
    .wrap { padding: 0 16px; }
    header { padding-top: 24px; }
    .moves { grid-template-columns: repeat(2, 1fr); }
    .premarket-grid { grid-template-columns: repeat(2, 1fr); }
    .scenario-grid { grid-template-columns: 1fr; }
    .portfolio-row { grid-template-columns: 1fr; gap: 3px; }
    .event-topline { align-items: center; flex-wrap: wrap; gap: 3px 6px; }
    .event-summary, .group-summary, .special-summary { padding: 12px; }
    .event-card h3 { font-size: 17px; }
    .event-watch { -webkit-line-clamp: 1; }
    .event-expanded, .group-body, .special-body { padding-left: 12px; padding-right: 12px; }
    .special-grid { grid-template-columns: 1fr; }
    .move-value { font-size: 20px; }
    .story { grid-template-columns: 24px 1fr; }
  }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="eyebrow">EVIDENCE-FIRST · US MARKETS</div>
    <h1>Market Brief</h1>
    <div class="date">$date_long · $mode_label</div>
    <div class="refresh-status" id="refresh-status"><span class="refresh-dot" aria-hidden="true"></span><span id="refresh-status-text">后台目标每 15 分钟更新 · 本页每分钟检查新版本 · 更新于 $generated_clock</span></div>
  </header>
  $weekly_events_section
  $premarket_section
  $market_section
  $sector_section
  $special_section
  $world_section
  <footer>Generated $generated_et · America/New_York<br>
  盘前行情至少延迟 16 分钟；收盘复盘基于最近完整交易日和同日新闻证据，不构成投资建议。</footer>
</div>
<script>
(() => {
  const currentVersion = document.documentElement.dataset.generatedAt;
  const statusText = document.getElementById("refresh-status-text");
  const statusWrap = document.getElementById("refresh-status");
  const generatedClock = "$generated_clock";

  function renderFreshness() {
    const generatedAt = Date.parse(currentVersion);
    if (!Number.isFinite(generatedAt)) return;
    const ageMinutes = Math.max(0, Math.floor((Date.now() - generatedAt) / 60000));
    statusWrap.classList.toggle("delayed", ageMinutes >= 25 && ageMinutes < 60);
    statusWrap.classList.toggle("stale", ageMinutes >= 60);
    if (ageMinutes < 25) {
      const age = ageMinutes < 1 ? "刚刚" : ageMinutes + " 分钟前";
      statusText.textContent = "更新于 " + generatedClock + " · " + age + " · 后台目标每15分钟";
    } else if (ageMinutes < 60) {
      statusText.textContent = "后台稍有延迟 · 更新于 " + generatedClock + " · 已 " + ageMinutes + " 分钟";
    } else {
      const ageHours = Math.floor(ageMinutes / 60);
      const remainder = ageMinutes % 60;
      statusText.textContent = "后台数据已延迟 " + ageHours + "小时" + remainder + "分钟 · 更新于 " + generatedClock + " · 本页每分钟重试";
    }
  }

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

  renderFreshness();
  checkForUpdate();
  window.setInterval(() => {
    renderFreshness();
    checkForUpdate();
  }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) checkForUpdate();
  });
})();
</script>
</body>
</html>
""")

ICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 192 192">
  <rect width="192" height="192" rx="42" fill="#ffffff"/>
  <path d="M34 126 L67 97 L91 110 L126 66 L159 82" fill="none" stroke="#15724c" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/>
  <circle cx="126" cy="66" r="9" fill="#9a5b2b"/>
  <rect x="1" y="1" width="190" height="190" rx="41" fill="none" stroke="#e4e3dc" stroke-width="2"/>
</svg>
"""

MANIFEST_JSON = """{
  "name": "Evidence-First Market Brief",
  "short_name": "Market Brief",
  "description": "Daily US market moves with evidence-ranked explanations",
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


def _brief_mode(now: datetime) -> str:
    local = now.astimezone(NY_TZ)
    if local.weekday() >= 5:
        return "周末新闻"
    if PREMARKET_START_ET <= local.time() < REGULAR_MARKET_OPEN_ET:
        return "盘前简报"
    return "收盘复盘" if local.time() >= time(16, 30) else "晨间简报"


def render_html(
    pulse: MarketPulse,
    assessment: DriverAssessment,
    world_articles: list[dict],
    output_dir: Path,
    now: datetime,
    overview: MarketOverview | None = None,
    sector_groups: list[SectorGroup] | None = None,
    special_news: dict[str, list[dict]] | None = None,
    premarket: PremarketPulse | None = None,
    weekly_calendar: WeeklyEventCalendar | None = None,
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
    weekly_calendar = weekly_calendar or load_weekly_event_calendar(now)
    page = HTML_TEMPLATE.safe_substitute(
        date_short=local.strftime("%-m/%-d"),
        date_long=local.strftime("%A · %B %-d, %Y"),
        mode_label=_brief_mode(now),
        generated_iso=now.astimezone(timezone.utc).isoformat(),
        generated_et=local.strftime("%Y-%m-%d %-I:%M %p ET"),
        generated_clock=local.strftime("%-I:%M %p ET"),
        premarket_section=_premarket_section_html(premarket),
        weekly_events_section=_weekly_events_html(weekly_calendar, now),
        market_section=_market_section_html(pulse, assessment, overview),
        sector_section=_sector_groups_html(sector_groups, pulse, premarket),
        special_section=_special_sections_html(pulse, special_news, premarket),
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
        "refresh_policy": {
            "backend_target_minutes": WEB_REFRESH_TARGET_MINUTES,
            "browser_check_minutes": 1,
            "scheduler": "GitHub Actions best effort",
        },
        "mode": _brief_mode(now),
        "market_session": pulse.session_date.isoformat() if pulse.session_date else None,
        "overview": overview.label_zh,
        "weekly_events": {
            "week_start": weekly_calendar.week_start.isoformat(),
            "week_end": weekly_calendar.week_end.isoformat(),
            "verified_at": (
                weekly_calendar.verified_at.isoformat()
                if weekly_calendar.verified_at else None
            ),
            "portfolio_symbols": list(weekly_calendar.portfolio_symbols),
            "events": [
                {
                    "id": event.event_id,
                    "starts_at": event.starts_at.isoformat(),
                    "title_zh": event.title_zh,
                    "importance": event.importance,
                    "source": event.source,
                    "source_url": event.source_url,
                    "state": (
                        "released"
                        if event.result is not None
                        else "pending_result"
                        if event.starts_at + timedelta(minutes=EVENT_RESULT_GRACE_MINUTES) <= now
                        else "upcoming"
                    ),
                    "result": (
                        _event_result_to_payload(event.result)
                        if event.result is not None else None
                    ),
                }
                for event in weekly_calendar.events
            ],
        },
        "premarket": {
            "active": bool(premarket and premarket.active),
            "as_of": premarket.as_of.isoformat() if premarket and premarket.as_of else None,
            "feed": premarket.feed if premarket else "unavailable",
            "moves": {
                symbol: {
                    "price": round(move.price, 4),
                    "previous_close": round(move.previous_close, 4),
                    "change_pct": round(move.change_pct, 4),
                    "volume": move.volume,
                    "high": round(move.high, 4),
                    "low": round(move.low, 4),
                    "as_of": move.as_of.isoformat(),
                }
                for symbol, move in (premarket.moves.items() if premarket else [])
            },
        },
    }
    (output_dir / "status.json").write_text(
        json.dumps(status, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    _write_event_results_cache(
        weekly_calendar,
        output_dir / EVENT_RESULTS_CACHE_NAME,
        now,
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
    premarket: PremarketPulse | None = None,
    weekly_calendar: WeeklyEventCalendar | None = None,
) -> list[tuple[str, str, str | None]]:
    local = now.astimezone(NY_TZ)
    brief_mode = _brief_mode(now)
    if brief_mode == "收盘复盘":
        mode = "Market Close"
    elif brief_mode == "盘前简报":
        mode = "Premarket Brief"
    else:
        mode = "Morning Brief"
    overview = overview or build_market_overview(pulse, assessment)
    sector_groups = sector_groups or []
    special_news = special_news or {symbol: [] for symbol in SPECIAL_WATCH_SPECS}
    weekly_calendar = weekly_calendar or load_weekly_event_calendar(now)
    title = f"{mode} · {overview.label_zh} — {local.strftime('%a %-m/%-d')}"
    lines: list[str] = []
    if premarket and premarket.available:
        as_of = (
            premarket.as_of.astimezone(NY_TZ).strftime("%-I:%M %p ET")
            if premarket.as_of else "时间未知"
        )
        lines.append(f"【盘前行情 · 截至 {as_of}】")
        for symbol in PREMARKET_SYMBOLS:
            move = premarket.moves.get(symbol)
            if move:
                lines.append(
                    f"{symbol} ${move.price:,.2f} {_format_pct(move.change_pct)} "
                    f"量 {_format_volume(move.volume)}"
                )
        lines.append(f"{premarket.note}；涨跌相对昨收。")

    if weekly_calendar.events:
        released = sorted(
            (event for event in weekly_calendar.events if event.result is not None),
            key=lambda event: event.result.published_at,
            reverse=True,
        )
        upcoming = [
            event for event in weekly_calendar.events if event.starts_at > now
        ]
        shown_events = released[:2]
        shown_events.extend(upcoming[: max(0, 3 - len(shown_events))])
        if not shown_events:
            shown_events = weekly_calendar.events[-2:]
        lines.append("【本周关键事件】")
        for event in shown_events:
            symbol_groups = [
                "/".join(impact.symbols) + f"({impact.sensitivity})"
                for impact in event.portfolio_impacts
                if impact.symbols
            ]
            if event.result is not None:
                lines.append(
                    f"▸ {event.time_label_zh} · {event.title_zh} · "
                    f"已公布：{event.result.verdict_zh}"
                )
                lines.append(f"结果：{event.result.summary_zh}")
                lines.append(f"仓位：{event.result.portfolio_impact_zh}")
                continue
            lines.append(f"▸ {event.time_label_zh} · {event.title_zh}")
            if event.bullish_sectors:
                lines.append(f"利好情景关注：{'、'.join(event.bullish_sectors[:4])}")
            if event.bearish_sectors:
                lines.append(f"利空情景关注：{'、'.join(event.bearish_sectors[:4])}")
            if symbol_groups:
                lines.append(f"仓位敏感度：{'；'.join(symbol_groups)}")

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
        premarket_move = premarket.moves.get(symbol) if premarket else None
        move = pulse.moves.get(symbol)
        if premarket_move:
            move_text = f"盘前 ${premarket_move.price:,.2f} {_format_pct(premarket_move.change_pct)}"
        elif move:
            move_text = f"昨收 ${move.close:,.2f} {_format_pct(move.change_pct)}"
        else:
            move_text = "行情暂不可用"
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
            premarket_move = premarket.moves.get(group.proxy_symbol or "") if premarket else None
            move = pulse.moves.get(group.proxy_symbol or "")
            if premarket_move:
                move_text = f" 盘前 {premarket_move.symbol} {_format_pct(premarket_move.change_pct)}"
            elif move:
                move_text = f" 昨收 {move.symbol} {_format_pct(move.change_pct)}"
            else:
                move_text = ""
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
    """Allow every timezone-aware silent refresh; notification gates stay separate."""
    return now.tzinfo is not None


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


def _demo_premarket(pulse: MarketPulse, now: datetime) -> PremarketPulse:
    session_date = _premarket_session_date(now)
    if session_date is None:
        return PremarketPulse(session_date=None)
    demo_changes = {
        "SPY": 0.42,
        "QQQ": 0.68,
        "IWM": -0.18,
        "SMH": 1.05,
        "MRVL": 0.91,
        "SPCX": -0.35,
    }
    moves: dict[str, PremarketMove] = {}
    as_of = now - timedelta(minutes=MARKET_DATA_DELAY_MINUTES)
    for index, symbol in enumerate(PREMARKET_SYMBOLS, 1):
        prior = pulse.moves.get(symbol)
        if not prior:
            continue
        change_pct = demo_changes[symbol]
        price = prior.close * (1 + change_pct / 100)
        moves[symbol] = PremarketMove(
            symbol=symbol,
            label=MARKET_SYMBOLS.get(symbol, symbol),
            price=price,
            previous_close=prior.close,
            change_pct=change_pct,
            volume=24_000 * index,
            high=price * 1.002,
            low=price * 0.998,
            as_of=as_of,
        )
    return PremarketPulse(
        session_date=session_date,
        moves=moves,
        status=_classify_premarket(moves),
        feed="sip",
        as_of=as_of,
        note=f"演示用延迟 SIP 盘前数据（至少延迟 {MARKET_DATA_DELAY_MINUTES} 分钟）",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-push", action="store_true", help="Render without sending ntfy notifications")
    parser.add_argument("--scheduled", action="store_true", help="Apply ET/DST schedule gate")
    parser.add_argument(
        "--web-refresh",
        action="store_true",
        help="Apply the daytime ET gate for silent website updates",
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
    weekly_calendar = merge_cached_event_results(
        load_weekly_event_calendar(now),
        args.output_dir / EVENT_RESULTS_CACHE_NAME,
        now,
    )

    if args.demo:
        pulse, assessment, world_articles = _demo_payload(now)
        premarket = _demo_premarket(pulse, now)
        path = render_html(
            pulse,
            assessment,
            world_articles,
            args.output_dir,
            now,
            premarket=premarket,
            weekly_calendar=weekly_calendar,
        )
        print(f"Rendered demo site: {path}")
        return 0

    if args.placeholder:
        pulse = MarketPulse(
            session_date=None,
            note="代码已升级；等待下一次使用有效 Alpaca 凭据生成经过验证的行情与新闻。",
        )
        assessment = build_driver_assessment(pulse, [])
        path = render_html(
            pulse,
            assessment,
            [],
            args.output_dir,
            now,
            weekly_calendar=weekly_calendar,
        )
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
    print(
        f"Weekly events: {weekly_calendar.week_start.isoformat()} to "
        f"{weekly_calendar.week_end.isoformat()} · {len(weekly_calendar.events)} verified"
    )
    premarket = fetch_premarket_pulse(api_key, api_secret, pulse, now)
    if premarket.active:
        print(
            "Premarket pulse:",
            premarket.session_date.isoformat() if premarket.session_date else "unavailable",
            premarket.status,
            premarket.feed,
            f"{len(premarket.moves)} symbols",
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

    weekly_calendar = update_weekly_event_results(
        weekly_calendar,
        market_articles,
        now,
    )

    drivers = select_market_drivers(market_articles, pulse)
    current_market_articles = current_section_news(market_articles, now)
    current_world_candidates = current_section_news(world_candidates, now)
    current_urls = {
        article.get("url") or "" for article in current_market_articles
    }
    current_drivers = [
        article for article in drivers if (article.get("url") or "") in current_urls
    ]
    world_articles = select_world_articles(current_world_candidates, now)
    assessment = build_driver_assessment(pulse, drivers)
    overview = build_market_overview(pulse, assessment)
    special_news = {
        symbol: select_special_news(current_market_articles, spec, now)
        for symbol, spec in SPECIAL_WATCH_SPECS.items()
    }
    special_urls = {
        article.get("url") or ""
        for articles in special_news.values()
        for article in articles
    }
    sector_groups = build_sector_groups(
        current_market_articles,
        current_drivers,
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
        premarket=premarket,
        weekly_calendar=weekly_calendar,
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
        premarket=premarket,
        weekly_calendar=weekly_calendar,
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
