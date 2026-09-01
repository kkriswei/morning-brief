# Evidence-First Market Brief

A bilingual US-market and world-news brief delivered as a PWA and through ntfy.

The market section no longer treats a popular headline as the reason stocks moved. Every run now follows this order:

1. Fetch comparable daily bars for SPY, QQQ, DIA, IWM, major sectors, Treasury/oil/dollar ETF proxies, SPCX, and MRVL.
2. During weekday premarket hours, fetch delayed 1-minute extended-hours bars for SPY, QQQ, IWM, SMH, MRVL, and SPCX.
3. Load the source-verified calendar for the current market week (or the coming week on weekends), with conditional sector and monitored-position impact.
4. Select the latest **completed** US trading session in `America/New_York`.
5. Fetch up to ten pages of Alpaca/Benzinga news plus independent market and primary-data feeds.
6. Match stories to the same session, actual index direction, broad-market language, and catalyst category.
7. Produce a Chinese bullish/bearish overview from actual breadth, not headline tone.
8. Group only the strongest stories into sector buckets, then render SPCX/SpaceX-related and MRVL news in dedicated sections.
9. Show source links and confidence. If evidence is insufficient, the brief says so instead of inventing a cause.

## Brief structure

The published page uses a white, compact reading view. It shows the decision-useful line first; supporting evidence, scenarios, position impact, market-data notes, and story summaries stay collapsed until the reader opens them.

1. **本周关键事件** — verified ET date/time and one-line watch item first; bullish/bearish scenarios, affected sectors, and monitored-position sensitivity expand on click.
2. **盘前行情** — delayed extended-hours price versus prior close first; cumulative volume, range, feed, and timestamp expand on click.
3. **今日总览** — `偏利好`, `偏利空`, `中性偏利好/利空`, or `中性分化`, backed by the four broad-index proxies and sector breadth.
4. **板块核心新闻** — each sector shows one headline preview; open it for up to two high-signal stories and their summaries.
5. **SPCX / SpaceX 相关** — the quote and newest headline stay visible; the dedicated watch list expands on click. Related SpaceX coverage is not automatically described as direct SPCX fundamentals.
6. **MRVL · Marvell** — the independent quote and newest headline stay visible; detailed Marvell/semiconductor coverage expands on click.
7. **全球重大新闻** — headlines stay scannable and separate; summaries and original titles expand on click.

## Data sources

### Market prices

- Alpaca historical daily bars.
- Alpaca delayed 1-minute bars from 4:00 AM ET during the weekday premarket window.
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

### Weekly event calendar

- BLS official release calendar for labor-market releases.
- Federal Reserve official monthly calendar for the Beige Book and policy events.
- ISM official Manufacturing and Services PMI release calendar.
- Company investor-relations announcements for selected high-impact earnings.

`data/weekly_events.json` is source-verified and time-bounded. The renderer shows only the active market week, so an expired event cannot silently roll forward as current. On Saturday and Sunday, “本周” means the coming Monday-through-Sunday market week.

The current monitored-symbol list is `SMH / VOO / QQQM / MRVL / SPCX`. Only direction and sensitivity are published; weights, cost basis, and account values are not stored. Update the JSON when the monitored list or next verified calendar changes. A JSON-only push also triggers regeneration.

### World news

- New York Times.
- BBC.
- NPR.

One failed feed does not fabricate a replacement. Confidence falls when fewer independent sources are available.

## Hosting and automatic refresh

The live page is hosted on GitHub Pages at:

https://kkriswei.github.io/morning-brief/

GitHub Actions silently regenerates the page once an hour, 24/7. During weekday premarket hours, the premarket block keeps current extended-hours prices separate from the latest completed session.

The weekend edition keeps Friday as the latest completed market session for the close recap, while sector, focus-list, and world sections accept only Saturday/Sunday stories. This prevents a high-scoring Friday recap from looking like current weekend news.

These website refreshes do not send ntfy notifications. An open browser checks `docs/status.json` every minute and reloads only when a newly generated brief has actually been deployed.

GitHub cron is best-effort and can start a few minutes late. The page therefore shows its generation time instead of claiming real-time data.

Premarket prices use delayed 1-minute Alpaca bars. SIP is preferred; IEX is an explicitly labeled fallback. Queries end at least 16 minutes before generation time so delayed subscriptions are never presented as real-time data.

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

The brief reports delayed market data and evidence-ranked news attribution. `偏利好/偏利空` describes the latest completed session's market breadth; it is not a forecast or a trade instruction. Weekly-event impacts are conditional scenarios, not predictions. Without position weights and cost basis, the portfolio section reports sensitivity rather than expected P&L. News reports can describe what investors cited, but they cannot prove a single unique cause for every market move. Flat or mixed sessions should often say **“没有单一主导催化剂”**.

## Files

| File | Purpose |
|---|---|
| `morning_brief.py` | Market bars, weekly calendar, news ranking, translations, HTML, and ntfy |
| `data/weekly_events.json` | Time-bounded, source-linked events and scenario/position impact |
| `tests/test_morning_brief.py` | Deterministic session, calendar, ranking, pagination, schedule, and render tests |
| `.github/workflows/morning-brief.yml` | Hourly silent refresh plus DST-aware weekday notification slots |
| `docs/index.html` | Generated PWA page |
| `docs/status.json` | Small deployment version marker used by browser auto-refresh |
| `docs/manifest.webmanifest` | PWA manifest |
| `docs/icon.svg` | PWA icon |
