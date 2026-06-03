# Morning Brief

Daily market + world news digest. Generated every morning at 9 AM ET, delivered three ways:

1. **Web app (PWA)** — installable on iPhone/Android home screen
2. **Push notification** — via [ntfy.sh](https://ntfy.sh) to your phone
3. **Plain HTML** — viewable at the GitHub Pages URL

Each story has an English summary (2-4 sentences) with the Chinese translation underneath for language practice. Stories are split into "World" (NYT/BBC/NPR top headlines) and "Markets" (Alpaca news feed, ranked for market impact).

## Setup

### 1. Create the repo

```bash
cd ~/Desktop/News
git init
git add .
git commit -m "Initial commit"
```

Then on GitHub, create a new **public** repo called `morning-brief` (or any name) and push:

```bash
git remote add origin https://github.com/<your-username>/morning-brief.git
git branch -M main
git push -u origin main
```

### 2. Add secrets

In your repo on GitHub: **Settings → Secrets and variables → Actions → New repository secret**.

Add three secrets:

| Name | Value |
|---|---|
| `ALPACA_API_KEY` | Your Alpaca paper-trading key |
| `ALPACA_API_SECRET` | Your Alpaca paper-trading secret |
| `NTFY_NEWS_TOPIC` | Any unique ntfy topic name (e.g. `morning-brief-XYZ123`) |

### 3. Enable GitHub Pages

**Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: **main** / folder: **`/docs`**
- Save

After the first workflow run finishes, your site will be live at:

```
https://<your-username>.github.io/morning-brief/
```

### 4. Trigger the first run

Go to **Actions → Morning Brief → Run workflow**. Confirm it completes green, then the site is live.

After this, it auto-runs daily at **13:00 UTC** (≈ 9 AM ET, give or take 15 min — GitHub cron is not exact).

### 5. Install on your phone

**iPhone (Safari):**
1. Open the GitHub Pages URL in Safari.
2. Tap the Share button → **Add to Home Screen**.
3. Done — it appears as an app icon and opens fullscreen.

**Android (Chrome):** look for the install prompt or use the menu → "Install app."

## Files

| File | Purpose |
|---|---|
| `morning_brief.py` | Main script: fetches news, ranks, scrapes summaries, translates, writes `docs/index.html`, pushes ntfy notifications |
| `.github/workflows/morning-brief.yml` | GitHub Actions cron job that runs the script and commits any changes to `docs/` |
| `docs/index.html` | The generated brief (overwritten each run) |
| `docs/manifest.webmanifest` | PWA manifest |
| `docs/icon.svg` | App icon |

## Running locally (optional)

If you want to test changes before pushing:

```bash
python3 -m venv .venv
.venv/bin/pip install requests beautifulsoup4
echo 'ALPACA_API_KEY=...'      > .env
echo 'ALPACA_API_SECRET=...'  >> .env
echo 'NTFY_NEWS_TOPIC=...'    >> .env
.venv/bin/python morning_brief.py
open docs/index.html
```

## Customization

| Want to change... | Edit |
|---|---|
| Fire time | `.github/workflows/morning-brief.yml` `cron:` line (UTC) |
| Number of articles | `MAX_ARTICLES` in `morning_brief.py` |
| Summary length | `LEAD_TARGET_CHARS` / `LEAD_MAX_CHARS` |
| Add/remove RSS feeds | `GENERAL_FEEDS` list |
| Source ranking | `score_article()` function |
| Web app theme | CSS variables at top of `HTML_TEMPLATE` |
