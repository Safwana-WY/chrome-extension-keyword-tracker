# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Maintenance:** Update this file whenever architecture, commands, configuration, or key behaviour changes.

## What this project does

Tracks Chrome Web Store keyword search positions for the **Accessibility Checker by WebToffee** extension and its competitors. Each daily run scrapes Chrome Web Store search results, records keyword positions and user counts, generates a tabbed HTML dashboard (`index.html`), and sends a Slack summary.

GitHub repo: https://github.com/Safwana-WY/chrome-extension-keyword-tracker
Dashboard (GitHub Pages): https://safwana-wy.github.io/chrome-extension-keyword-tracker/

## Extensions currently tracked

| Extension | ID | Competitors |
|---|---|---|
| Accessibility Checker by WebToffee | `nidjdackonjofdcclfbdcapbkgghcdjf` | Siteimprove, IBM Equal Access, WAVE, Silktide |

Competitor IDs:
- Siteimprove: `djcglbmbegflehmbfleechkjhmedcopn`
- IBM Equal Access: `lkcagbfjnkomcinoddgooolagloogehp`
- WAVE: `jbbplnpkjmmeebjpijfedlgcdilocofh`
- Silktide: `mpobacholfblmnpnfbiomjkecoojakah`

## Commands

```bash
# Full run: fetch positions + users, update dashboard, send Slack alert
python3 tracker.py

# Regenerate dashboard from existing data without hitting CWS
python3 tracker.py --dry

# Install dependency
pip3 install requests
```

## Daily automation (cron at 10am IST)

```
0 10 * * * cd /Users/safwanata/Desktop/ClaudeProjects/chrome-extension-keyword-tracker && /usr/bin/python3 tracker.py >> data/tracker.log 2>&1 && git add index.html data/positions.json && git commit -m "Daily update $(date +\%Y-\%m-\%d)" && git push >> data/tracker.log 2>&1
```

Cron is registered on the local machine. The Mac must be on and awake at 10am IST. Check `data/tracker.log` to confirm runs are succeeding.

## Architecture

**`tracker.py`** — single-file Python script with four responsibilities:
1. `run_check()` — scrapes CWS search pages for each keyword × extension ID, records 1-indexed positions; also calls `fetch_users()` per extension
2. `generate_dashboard()` — writes `index.html` as a self-contained tabbed static page (one tab per extension, no external dependencies)
3. `send_slack()` — posts a Block Kit message covering all extensions: users, changes, competitor wins/losses
4. `send_email()` — optional HTML email alert (disabled by default)

**How `check_position()` works:** Fetches `https://chromewebstore.google.com/search/{keyword}` with a browser User-Agent. Extracts all 32-character lowercase extension IDs from the HTML via regex (Chrome extension IDs are always exactly 32 `a-z` chars), deduplicates while preserving order, and returns the 1-based index of the target extension. Returns `None` if not in top `results_depth` (default 50).

**How `fetch_users()` works:** Fetches the CWS detail page and extracts user count, rating, and review count via regex. Stores as `_users`, `_rating`, `_reviews`.

**`config.json`** — single source of truth for all extension definitions, keywords, and notification settings. Currently tracking 17 keywords × 5 extensions; each run takes ~3 minutes (2s delay per request). Slack posts to `#chrome-extension-keyword-tracking`.

**Data flow:**
- Positions stored in `data/positions.json`: `{ "YYYY-MM-DD": { "ext-id": { "keyword": position, "_users": N, "_rating": F, "_reviews": N } } }`
- Keys starting with `_` are internal metadata; `keyword_positions()` helper strips them when processing rankings
- `index.html` committed to `main` and served via GitHub Pages
- Changes detected by comparing today vs yesterday per extension
- Week-on-week comparison shown on stat cards and keyword table; requires 7 days of data

**Secrets handling:**
- `secrets.json` (gitignored) holds `slack_webhook_url` and optionally `email_password`
- `load_config()` merges secrets at runtime — `config.json` has no credentials and is safe to commit

## Adding a new extension

Add an entry to the `extensions` array in `config.json`. No code changes required:

```json
{
  "id": "32-char-extension-id-here",
  "name": "Display Name",
  "competitors": [
    { "id": "32-char-competitor-id", "name": "Competitor Name" }
  ],
  "keywords": ["keyword one", "keyword two"]
}
```

## Git workflow

After any code or config changes, push to GitHub immediately:

```bash
git add <changed files>
git commit -m "Short description"
git push
```
