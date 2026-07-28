# Evidence-First Market Brief

A bilingual US-market and world-news brief delivered as a PWA and through ntfy.

The market section no longer treats a popular headline as the reason stocks moved. Every run now follows this order:

1. Fetch comparable daily bars for SPY, QQQ, DIA, IWM, major sectors, Treasury/oil/dollar ETF proxies, SPCX, and MRVL.
2. Select the latest **completed** US trading session in `America/New_York`.
3. Fetch up to ten pages of Alpaca/Benzinga news plus independent market and primary-data feeds.
4. Match stories to the same session, actual index direction, broad-market language, and catalyst category.
5. Produce a Chinese bullish/bearish overview from actual breadth, not headline tone.
6. Group only the strongest stories into sector buckets, then render SPCX/SpaceX-related and MRVL news in dedicated sections.
7. Show source links and confidence. If evidence is insufficient, the brief says so instead of inventing a cause.

## Brief structure

1. **今日总览** — `偏利好`, `偏利空`, `中性偏利好/利空`, or `中性分化`, backed by the four broad-index proxies and sector breadth.
2. **板块核心新闻** — up to two high-signal stories per selected sector, such as semiconductors, technology/AI, financials, energy, macro/rates, consumer, healthcare, and industrials.
3. **SPCX / SpaceX 相关** — SPCX quote plus stories matching the SPCX symbol or SpaceX, Starlink, and Starship keywords. Related SpaceX coverage is not automatically described as direct SPCX fundamentals.
4. **MRVL · Marvell** — an independent MRVL quote and dedicated Marvell/semiconductor news list.
5. **全球重大新闻** — kept separate so world stories cannot be misrepresented as the cause of a US-market move.

## Data sources

### Market prices

- Alpaca historical daily bars.
- Delayed consolidated SIP data is preferred.
- IEX single-exchange data is used only as a clearly labeled fallback.
- SPY, QQQ, DIA, and IWM are ETF proxies for the broad indexes.

### Market evidence

- Alpaca/Benzinga, paginated up to 500 articles so Monday can still reach Friday's session.
- Bloomberg Markets.
- Financial Times Markets.
- CNBC Markets.
- MarketWatch.
- New York Times Business.
- BBC Business.
- Federal Reserve releases.

### World news

- New York Times.
- BBC.
- NPR.

One failed feed does not fabricate a replacement. Confidence falls when fewer independent sources are available.

## Hosting and automatic refresh

The live page is hosted on GitHub Pages at:

https://kkriswei.github.io/morning-brief/

During weekday US-market hours, GitHub Actions silently regenerates the page about every 30 minutes. These website refreshes do not send ntfy notifications. An open browser checks `docs/status.json` every minute and reloads only when a newly generated brief has actually been deployed.

GitHub cron is best-effort and can start a few minutes late. The page therefore shows its generation time instead of claiming real-time data.

## Notification schedule

Phone notifications remain limited to two weekday Eastern Time windows:

- Around **9:00 AM ET**: explains the latest completed session and adds current overnight news.
- Around **4:45 PM ET**: explains the just-completed session.

GitHub cron uses UTC. The workflow includes both daylight- and standard-time candidates; `--scheduled` applies an `America/New_York` gate so the duplicate invocation exits without rendering or notifying.

On a market holiday, the duplicate after-close brief is skipped. The morning brief can still summarize the last completed session.

## Delivery

Each successful run can produce:

1. An installable web app in `docs/`.
2. An ntfy market notification containing the overview verdict, actual index-proxy changes, sector buckets, and dedicated SPCX/MRVL lines.
3. A separate ntfy world-news notification when qualifying stories exist.

If ntfy delivery fails after the page renders, the workflow remains visibly failed but still commits the updated page. A green workflow is therefore evidence that both generation and delivery completed.

## GitHub setup

Add these repository secrets under **Settings → Secrets and variables → Actions**:

| Secret | Value |
|---|---|
| `ALPACA_API_KEY` | Alpaca paper/live API key |
| `ALPACA_API_SECRET` | Matching Alpaca secret |
| `NTFY_NEWS_TOPIC` | A unique ntfy topic |

Enable GitHub Pages from the `main` branch and `/docs` directory.

The workflow has `workflow_dispatch`, so it can also be run manually from Actions. A manual run bypasses the schedule gate. Code pushes rebuild the live page without sending a phone notification.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install requests beautifulsoup4
```

Create `.env`:

```text
ALPACA_API_KEY=...
ALPACA_API_SECRET=...
NTFY_NEWS_TOPIC=...
```

Render with live data but do not send a phone notification:

```bash
.venv/bin/python morning_brief.py --no-push
```

Render a clearly synthetic, offline layout preview outside `docs/`:

```bash
.venv/bin/python morning_brief.py --demo --no-push --output-dir /tmp/market-brief-demo
```

Render a non-synthetic waiting page without credentials:

```bash
.venv/bin/python morning_brief.py --placeholder --no-push
```

Run tests:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## Important limitation

The brief reports delayed market data and evidence-ranked news attribution. `偏利好/偏利空` describes the latest completed session's market breadth; it is not a forecast or a trade instruction. News reports can describe what investors cited, but they cannot prove a single unique cause for every market move. Flat or mixed sessions should often say **“没有单一主导催化剂”**.

## Files

| File | Purpose |
|---|---|
| `morning_brief.py` | Market bars, news pagination, evidence ranking, translations, HTML, and ntfy |
| `tests/test_morning_brief.py` | Deterministic session, ranking, pagination, schedule, and render tests |
| `.github/workflows/morning-brief.yml` | DST-aware weekday morning/close automation |
| `docs/index.html` | Generated PWA page |
| `docs/status.json` | Small deployment version marker used by browser auto-refresh |
| `docs/manifest.webmanifest` | PWA manifest |
| `docs/icon.svg` | PWA icon |
