#!/usr/bin/env python3
"""
Chrome Web Store Keyword Position Tracker
Tracks search rankings and user counts for Chrome extensions and their competitors.

Usage:
  python tracker.py          # Full run: fetch positions + users, update dashboard, send Slack
  python tracker.py --dry    # Regenerate dashboard from existing data only (no API calls)

To add a new extension, add an entry to the "extensions" array in config.json.
Keyword positions are stored in data/positions.json.
User counts use the _users key alongside keyword positions.
"""

import json
import os
import re
import sys
import time
import requests
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

# ---------------------------------------------------------------------------
# PATHS
# ---------------------------------------------------------------------------
BASE_DIR       = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE    = os.path.join(BASE_DIR, "config.json")
SECRETS_FILE   = os.path.join(BASE_DIR, "secrets.json")
DATA_FILE      = os.path.join(BASE_DIR, "data", "positions.json")
DASHBOARD_FILE = os.path.join(BASE_DIR, "index.html")

CWS_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# CONFIG / DATA HELPERS
# ---------------------------------------------------------------------------

def load_config():
    with open(CONFIG_FILE) as f:
        config = json.load(f)

    # Merge secrets (gitignored)
    if os.path.exists(SECRETS_FILE):
        with open(SECRETS_FILE) as f:
            secrets = json.load(f)
        if secrets.get("slack_webhook_url"):
            config.setdefault("slack", {})["webhook_url"] = secrets["slack_webhook_url"]
        if secrets.get("email_password"):
            config.setdefault("email", {})["password"] = secrets["email_password"]

    return config


def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            return json.load(f)
    return {}


def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def keyword_positions(ext_data):
    """Return only keyword→position entries, skipping internal _keys."""
    return {k: v for k, v in ext_data.items() if not k.startswith("_")}


def format_users(n):
    if not isinstance(n, int):
        return "—"
    if n >= 1_000_000:
        return f"{n // 1_000_000}M+"
    if n >= 1_000:
        return f"{n // 1_000:,}K+"
    return f"{n:,}+"


def find_last_week_date(dates, today_str):
    """Return the date string exactly 7 days before today_str if it exists in dates, else None."""
    target = (datetime.strptime(today_str, "%Y-%m-%d") - timedelta(days=7)).strftime("%Y-%m-%d")
    return target if target in dates else None


def wow_trend(current, prev, label="vs last week"):
    """Return an HTML span showing week-on-week delta, or empty string if data unavailable."""
    if not isinstance(prev, int) or not isinstance(current, int):
        return ""
    d = current - prev
    if d > 0:
        return f'<span style="color:#16a34a;font-size:.75rem">▲ +{d} {label}</span>'
    if d < 0:
        return f'<span style="color:#dc2626;font-size:.75rem">▼ {d} {label}</span>'
    return f'<span style="color:#94a3b8;font-size:.75rem">→ same {label}</span>'


# ---------------------------------------------------------------------------
# CHROME WEB STORE SCRAPING
# ---------------------------------------------------------------------------

def check_position(keyword, extension_id, depth=50, delay=2.0):
    """Return 1-indexed position of extension_id in CWS search results, or None/error."""
    url = f"https://chromewebstore.google.com/search/{requests.utils.quote(keyword)}"
    try:
        time.sleep(delay)
        resp = requests.get(url, headers={"User-Agent": CWS_USER_AGENT}, timeout=20)
        resp.raise_for_status()
        # Extract 32-char lowercase extension IDs in order of appearance, deduplicated
        ids_found = list(dict.fromkeys(re.findall(r'\b([a-z]{32})\b', resp.text)))
        for i, eid in enumerate(ids_found[:depth]):
            if eid == extension_id:
                return i + 1
        return None
    except Exception as exc:
        print(f"    [ERROR] position '{keyword}' / '{extension_id}': {exc}")
        return "error"


def fetch_users(extension_id, delay=1.0):
    """Return (user_count, rating, review_count) from CWS detail page."""
    url = f"https://chromewebstore.google.com/detail/{extension_id}"
    try:
        time.sleep(delay)
        resp = requests.get(url, headers={"User-Agent": CWS_USER_AGENT}, timeout=20)
        resp.raise_for_status()
        html = resp.text

        # User count
        users = None
        for pat in [r'(\d[\d,]+)\+?\s+users', r'"userCount":"?(\d+)"?', r'(\d[\d,]+)\s+user']:
            m = re.search(pat, html, re.IGNORECASE)
            if m:
                users = int(m.group(1).replace(",", ""))
                break

        # Rating
        rating = None
        m = re.search(r'"averageRating"[:\s]+"?([\d.]+)"?', html)
        if m:
            rating = float(m.group(1))

        # Review count
        reviews = None
        m = re.search(r'"ratingCount"[:\s]+"?(\d+)"?', html)
        if m:
            reviews = int(m.group(1))

        return users, rating, reviews
    except Exception as exc:
        print(f"    [ERROR] users '{extension_id}': {exc}")
        return None, None, None


# ---------------------------------------------------------------------------
# POSITION CHECK RUN
# ---------------------------------------------------------------------------

def run_check(config):
    data       = load_data()
    today      = datetime.utcnow().strftime("%Y-%m-%d")
    extensions = config["extensions"]
    delay      = config.get("request_delay_seconds", 2.0)
    depth      = config.get("results_depth", 50)

    if today not in data:
        data[today] = {}

    all_changes = {}  # { ext_id: [changes] }

    for ext_cfg in extensions:
        ext_id      = ext_cfg["id"]
        ext_name    = ext_cfg.get("name", ext_id)
        keywords    = ext_cfg.get("keywords", [])
        competitors = ext_cfg.get("competitors", [])
        all_ids     = [ext_id] + [c["id"] for c in competitors]

        # Ensure today's entries exist
        for eid in all_ids:
            if eid not in data[today]:
                data[today][eid] = {}

        # Find yesterday for change detection
        dates_so_far = sorted(k for k in data if k != today)
        yesterday    = dates_so_far[-1] if dates_so_far else None

        changes = []

        print(f"\n{'─'*70}")
        print(f"  {ext_name}  ({ext_id})")
        print(f"  {len(keywords)} keywords × {len(all_ids)} extensions")
        print(f"{'─'*70}")
        header = f"  {'KEYWORD':<40}" + "".join(f"  {eid[:20]:<22}" for eid in all_ids)
        print(header)
        print("  " + "─" * (len(header) - 2))

        for keyword in keywords:
            row = f"  {keyword:<40}"
            for eid in all_ids:
                pos = check_position(keyword, eid, depth=depth, delay=delay)
                if pos != "error":
                    data[today][eid][keyword] = pos
                pos_str = f"#{pos}" if isinstance(pos, int) else (str(pos) if pos else "—")
                row += f"  {pos_str:<22}"
            print(row)

            # Change detection (primary extension only)
            if yesterday:
                prev    = keyword_positions(data[yesterday].get(ext_id, {})).get(keyword)
                current = keyword_positions(data[today].get(ext_id, {})).get(keyword)
                if isinstance(prev, int) and isinstance(current, int) and prev != current:
                    changes.append({"keyword": keyword, "prev": prev, "current": current})

        # Fetch user counts for all extensions
        print(f"\n  Fetching user counts...")
        for eid in all_ids:
            users, rating, reviews = fetch_users(eid, delay=delay)
            data[today][eid]["_users"]   = users
            data[today][eid]["_rating"]  = rating
            data[today][eid]["_reviews"] = reviews
            print(f"    {eid[:40]:<42}  {format_users(users)}"
                  + (f"  ★{rating}" if rating else ""))

        all_changes[ext_id] = changes
        print(f"\n  {len(changes)} position change(s) for '{ext_name}'.")

    save_data(data)
    print(f"\nAll data saved.")
    return data, all_changes


# ---------------------------------------------------------------------------
# DASHBOARD GENERATOR
# ---------------------------------------------------------------------------

def generate_dashboard(data, config):
    extensions     = config["extensions"]
    dates          = sorted(data.keys())
    today          = dates[-1] if dates else None
    yesterday      = dates[-2] if len(dates) >= 2 else None
    last_week_date = find_last_week_date(dates, today) if today else None

    tabs_html    = ""
    content_html = ""

    for e_idx, ext_cfg in enumerate(extensions):
        ext_id      = ext_cfg["id"]
        ext_name    = ext_cfg.get("name", ext_id)
        keywords    = ext_cfg.get("keywords", [])
        competitors = ext_cfg.get("competitors", [])

        our_raw     = data[today].get(ext_id, {}) if today else {}
        our_kws     = keyword_positions(our_raw)
        our_users   = our_raw.get("_users")
        our_rating  = our_raw.get("_rating")
        our_reviews = our_raw.get("_reviews")

        prev_raw   = data[yesterday].get(ext_id, {}) if yesterday else {}
        prev_users = prev_raw.get("_users")

        lw_raw = data[last_week_date].get(ext_id, {}) if last_week_date else {}
        lw_kws = keyword_positions(lw_raw)

        # ── Top 10 keywords ────────────────────────────────────────────────
        top10_kws = sorted(
            [(kw, v) for kw, v in our_kws.items() if isinstance(v, int) and v <= 10],
            key=lambda x: x[1]
        )
        lw_top10_count = sum(1 for v in lw_kws.values() if isinstance(v, int) and v <= 10)

        # ── Keywords #11–20 ────────────────────────────────────────────────
        pos11_20_kws = sorted(
            [(kw, v) for kw, v in our_kws.items() if isinstance(v, int) and 11 <= v <= 20],
            key=lambda x: x[1]
        )
        lw_pos11_20_count = sum(1 for v in lw_kws.values() if isinstance(v, int) and 11 <= v <= 20)

        # ── WoW improved / declined ────────────────────────────────────────
        improved_kws = []
        declined_kws = []
        if last_week_date:
            for kw in keywords:
                curr   = our_kws.get(kw)
                lw_pos = lw_kws.get(kw)
                if isinstance(curr, int) and isinstance(lw_pos, int):
                    if curr < lw_pos:
                        improved_kws.append((kw, lw_pos, curr))
                    elif curr > lw_pos:
                        declined_kws.append((kw, lw_pos, curr))
        improved_kws.sort(key=lambda x: x[2])   # best position first
        declined_kws.sort(key=lambda x: -x[2])  # worst position first

        # ── Users trend ────────────────────────────────────────────────────
        if isinstance(our_users, int) and isinstance(prev_users, int):
            users_delta = our_users - prev_users
            if users_delta > 0:
                users_trend = f'<span style="color:#16a34a;font-size:.75rem">▲ +{format_users(users_delta)}</span>'
            elif users_delta < 0:
                users_trend = f'<span style="color:#dc2626;font-size:.75rem">▼ {format_users(abs(users_delta))}</span>'
            else:
                users_trend = '<span style="color:#94a3b8;font-size:.75rem">No change</span>'
        else:
            users_trend = ""

        # ── Top 10 WoW label ───────────────────────────────────────────────
        if last_week_date:
            top10_delta = len(top10_kws) - lw_top10_count
            if top10_delta > 0:
                top10_wow = f'<span style="color:#16a34a;font-size:.75rem">▲ +{top10_delta} vs last week</span>'
            elif top10_delta < 0:
                top10_wow = f'<span style="color:#dc2626;font-size:.75rem">▼ {abs(top10_delta)} vs last week</span>'
            else:
                top10_wow = '<span style="color:#94a3b8;font-size:.75rem">→ same vs last week</span>'
        else:
            top10_wow = ""

        # ── #11–20 WoW label ───────────────────────────────────────────────
        if last_week_date:
            pos11_20_delta = len(pos11_20_kws) - lw_pos11_20_count
            if pos11_20_delta > 0:
                pos11_20_wow = f'<span style="color:#16a34a;font-size:.75rem">▲ +{pos11_20_delta} vs last week</span>'
            elif pos11_20_delta < 0:
                pos11_20_wow = f'<span style="color:#dc2626;font-size:.75rem">▼ {abs(pos11_20_delta)} vs last week</span>'
            else:
                pos11_20_wow = '<span style="color:#94a3b8;font-size:.75rem">→ same vs last week</span>'
        else:
            pos11_20_wow = ""

        # ── Reviews / rating display ───────────────────────────────────────
        rating_display  = f"{our_rating:.1f}" if isinstance(our_rating, float) else "—"
        reviews_display = f"{our_reviews:,}" if isinstance(our_reviews, int) else "—"
        cws_reviews_url = f"https://chromewebstore.google.com/detail/{ext_id}/reviews"

        # ── Top 10 panel content ───────────────────────────────────────────
        if top10_kws:
            top10_rows = ""
            for kw, pos in top10_kws:
                lw_p = lw_kws.get(kw)
                if isinstance(lw_p, int):
                    d = lw_p - pos
                    if d > 0:
                        wow = f'<span style="color:#16a34a">▲ +{d}</span>'
                    elif d < 0:
                        wow = f'<span style="color:#dc2626">▼ {abs(d)}</span>'
                    else:
                        wow = '<span style="color:#94a3b8">→</span>'
                else:
                    wow = '<span style="color:#94a3b8">—</span>'
                top10_rows += (
                    f'<tr>'
                    f'<td style="padding:8px 16px;font-weight:500">{kw}</td>'
                    f'<td style="padding:8px 16px;font-weight:700;color:#16a34a">#{pos}</td>'
                    f'<td style="padding:8px 16px">{wow}</td>'
                    f'</tr>'
                )
            top10_panel_body = f"""<table style="width:100%;border-collapse:collapse">
              <thead><tr style="background:#f8fafc">
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">Keyword</th>
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">Position</th>
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">vs Last Week</th>
              </tr></thead>
              <tbody>{top10_rows}</tbody>
            </table>"""
        else:
            top10_panel_body = '<p style="padding:20px;color:#94a3b8;text-align:center;font-size:.85rem">No keywords ranking in top 10</p>'

        # ── #11–20 panel content ───────────────────────────────────────────
        if pos11_20_kws:
            pos11_20_rows = ""
            for kw, pos in pos11_20_kws:
                lw_p = lw_kws.get(kw)
                if isinstance(lw_p, int):
                    d = lw_p - pos
                    if d > 0:
                        wow = f'<span style="color:#16a34a">▲ +{d}</span>'
                    elif d < 0:
                        wow = f'<span style="color:#dc2626">▼ {abs(d)}</span>'
                    else:
                        wow = '<span style="color:#94a3b8">→</span>'
                else:
                    wow = '<span style="color:#94a3b8">—</span>'
                pos11_20_rows += (
                    f'<tr>'
                    f'<td style="padding:8px 16px;font-weight:500">{kw}</td>'
                    f'<td style="padding:8px 16px;font-weight:700;color:#2563eb">#{pos}</td>'
                    f'<td style="padding:8px 16px">{wow}</td>'
                    f'</tr>'
                )
            pos11_20_panel_body = f"""<table style="width:100%;border-collapse:collapse">
              <thead><tr style="background:#f8fafc">
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">Keyword</th>
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">Position</th>
                <th style="padding:8px 16px;text-align:left;font-size:.68rem;text-transform:uppercase;letter-spacing:.06em;color:#94a3b8;border-bottom:1px solid #e2e8f0">vs Last Week</th>
              </tr></thead>
              <tbody>{pos11_20_rows}</tbody>
            </table>"""
        else:
            pos11_20_panel_body = '<p style="padding:20px;color:#94a3b8;text-align:center;font-size:.85rem">No keywords ranking in positions 11–20</p>'

        # ── WoW highlight box ──────────────────────────────────────────────
        if not last_week_date:
            wow_box = (
                '<div style="background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px;'
                'padding:20px;margin-bottom:28px;text-align:center;color:#94a3b8;font-size:.85rem">'
                'Not enough data for week-on-week comparison (need 7 days of history)</div>'
            )
        else:
            improved_items = "".join(
                f'<li>{kw} <span style="color:#94a3b8;font-size:.8rem">#{pp} → <strong style="color:#16a34a">#{cp}</strong></span></li>'
                for kw, pp, cp in improved_kws
            ) or '<li style="color:#94a3b8;font-size:.85rem">No improvements this week</li>'

            declined_items = "".join(
                f'<li>{kw} <span style="color:#94a3b8;font-size:.8rem">#{pp} → <strong style="color:#dc2626">#{cp}</strong></span></li>'
                for kw, pp, cp in declined_kws
            ) or '<li style="color:#94a3b8;font-size:.85rem">No declines this week</li>'

            wow_box = f"""<div class="wow-box">
            <div class="wow-col">
              <div class="wow-section-title" style="color:#16a34a">
                <span class="wow-count">{len(improved_kws)}</span> Keywords Improved
              </div>
              <ul class="wow-list">{improved_items}</ul>
            </div>
            <div class="wow-divider"></div>
            <div class="wow-col">
              <div class="wow-section-title" style="color:#dc2626">
                <span class="wow-count">{len(declined_kws)}</span> Keywords Declined
              </div>
              <ul class="wow-list">{declined_items}</ul>
            </div>
          </div>"""

        # ── Sort keywords ──────────────────────────────────────────────────
        def sort_key(kw):
            pos = our_kws.get(kw)
            return (0, pos) if isinstance(pos, int) else (1, 9999)
        sorted_kws = sorted(keywords, key=sort_key)

        # ── Keyword position history table ─────────────────────────────────
        hist_headers = "".join(f'<th class="hist-header">{d[5:]}</th>' for d in dates[-7:])

        kw_rows = ""
        for kw in sorted_kws:
            our = our_kws.get(kw)
            if not isinstance(our, int):
                our_cell  = '<span class="pos-none">Not in top 50</span>'
                row_class = "row-none"
            else:
                our_cell  = f'<span class="pos-num">#{our}</span>'
                row_class = ""

            hist_cells = ""
            for date in dates[-7:]:
                pos = keyword_positions(data[date].get(ext_id, {})).get(kw)
                if isinstance(pos, int):
                    cls = "hist-top10" if pos <= 10 else ("hist-top30" if pos <= 30 else "")
                    hist_cells += f'<td class="hist-cell {cls}">#{pos}</td>'
                else:
                    hist_cells += '<td class="hist-cell hist-none">—</td>'

            kw_rows += f"""
            <tr class="{row_class}">
              <td class="kw-cell">{kw}</td>
              <td class="pos-cell">{our_cell}</td>
              {hist_cells}
            </tr>"""

        # ── Competitor positions table ─────────────────────────────────────
        comp_col_headers = "".join(
            f'<th colspan="2" style="text-align:center;border-left:2px solid #e2e8f0">{c["name"]}</th>'
            for c in competitors
        )
        comp_subheaders = "".join(
            '<th style="text-align:center;border-left:2px solid #e2e8f0;font-size:.65rem">Current</th>'
            '<th style="text-align:center;font-size:.65rem">vs Last Week</th>'
            for _ in competitors
        )

        comp_rows = ""
        for kw in sorted_kws:
            row_parts = f'<td class="kw-cell">{kw}</td>'
            for comp in competitors:
                c_kws_today = keyword_positions(data[today].get(comp["id"], {})) if today else {}
                c_kws_lw    = keyword_positions(data[last_week_date].get(comp["id"], {})) if last_week_date else {}
                c_pos = c_kws_today.get(kw)
                c_lw  = c_kws_lw.get(kw)
                border = 'style="border-left:2px solid #e2e8f0"'
                if not isinstance(c_pos, int):
                    row_parts += f'<td class="comp-cell comp-none" {border}>—</td><td class="comp-cell comp-none">—</td>'
                else:
                    if isinstance(c_lw, int):
                        d = c_lw - c_pos
                        if d > 0:
                            wow_html = f'<span style="color:#16a34a;font-size:.75rem">▲ +{d}</span>'
                        elif d < 0:
                            wow_html = f'<span style="color:#dc2626;font-size:.75rem">▼ {abs(d)}</span>'
                        else:
                            wow_html = '<span style="color:#94a3b8;font-size:.75rem">→</span>'
                    else:
                        wow_html = '<span style="color:#94a3b8;font-size:.75rem">—</span>'
                    row_parts += f'<td class="comp-cell" {border}>#{c_pos}</td><td class="comp-cell">{wow_html}</td>'
            comp_rows += f'<tr>{row_parts}</tr>'

        # ── Assemble tab ───────────────────────────────────────────────────
        active_cls = "active" if e_idx == 0 else ""
        tabs_html += f'<button class="tab {active_cls}" onclick="showTab({e_idx}, this)">{ext_name}</button>'

        content_html += f"""
        <div id="tab-{e_idx}" class="tab-content {active_cls}">

          <!-- Stats row -->
          <div class="stats">
            <div class="card c-blue">
              <div class="value">{format_users(our_users)}</div>
              <div class="label">Active Installations</div>
              <div style="margin-top:4px;min-height:16px">{users_trend}</div>
            </div>
            <a href="{cws_reviews_url}" target="_blank" rel="noopener" class="card card-link">
              <div class="value">{reviews_display}</div>
              <div class="label">Reviews</div>
              <div style="margin-top:6px;font-size:.9rem;color:#f59e0b;font-weight:700">★ {rating_display}</div>
            </a>
            <div class="card card-clickable c-green" onclick="togglePanel('panel-top10-{e_idx}')">
              <div class="value">{len(top10_kws)}</div>
              <div class="label">Keywords in Top 10 ↗</div>
              <div style="margin-top:4px;min-height:16px">{top10_wow}</div>
            </div>
            <div class="card card-clickable" onclick="togglePanel('panel-pos1120-{e_idx}')">
              <div class="value">{len(pos11_20_kws)}</div>
              <div class="label">Keywords #11–20 ↗</div>
              <div style="margin-top:4px;min-height:16px">{pos11_20_wow}</div>
            </div>
          </div>

          <!-- Top 10 expandable panel -->
          <div id="panel-top10-{e_idx}" class="expand-panel" style="display:none">
            <div class="table-title">Keywords Ranking in Top 10</div>
            {top10_panel_body}
          </div>

          <!-- #11–20 expandable panel -->
          <div id="panel-pos1120-{e_idx}" class="expand-panel" style="display:none">
            <div class="table-title">Keywords Ranking #11–20</div>
            {pos11_20_panel_body}
          </div>

          <!-- WoW highlight box -->
          <div class="section-title">Week-on-Week Changes</div>
          {wow_box}

          <!-- Keyword position history -->
          <div class="section-title">Keyword Position History</div>
          <div class="table-wrap" style="margin-bottom:28px">
            <div class="table-title">
              <span>Daily positions for all tracked keywords</span>
              <div class="legend">
                <span><span class="legend-dot" style="background:#16a34a"></span>Top 10</span>
                <span><span class="legend-dot" style="background:#2563eb"></span>Top 30</span>
              </div>
            </div>
            <table>
              <thead><tr>
                <th>Keyword</th><th>Current</th>{hist_headers}
              </tr></thead>
              <tbody>{kw_rows}</tbody>
            </table>
          </div>

          <!-- Competitor keyword positions -->
          <div class="section-title">Competitor Keyword Positions</div>
          <div class="table-wrap">
            <div class="table-title">
              <span>Current positions and week-on-week change per keyword</span>
            </div>
            <table>
              <thead>
                <tr>
                  <th rowspan="2" style="vertical-align:bottom">Keyword</th>
                  {comp_col_headers}
                </tr>
                <tr>{comp_subheaders}</tr>
              </thead>
              <tbody>{comp_rows}</tbody>
            </table>
          </div>

        </div>"""

    last_updated = (datetime.utcnow() + timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d %H:%M IST")
    ext_count    = len(extensions)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>CWS Keyword Tracker</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f1f5f9; color: #1e293b; font-size: 14px; }}

    /* ── Header ── */
    .header {{ background: linear-gradient(135deg, #1d4ed8, #2563eb); color: #fff;
               padding: 18px 32px; display: flex; justify-content: space-between; align-items: center; }}
    .header h1 {{ font-size: 1.15rem; font-weight: 700; letter-spacing: -.02em; }}
    .header p  {{ font-size: 0.78rem; opacity: .72; margin-top: 3px; }}
    .header .updated {{ font-size: 0.72rem; opacity: .6; text-align: right; }}

    /* ── Tabs ── */
    .tab-bar {{ background: #1e40af; padding: 0 32px; display: flex; gap: 2px; }}
    .tab {{ background: transparent; border: none; color: rgba(255,255,255,.65);
             padding: 12px 20px; font-size: .85rem; font-weight: 500; cursor: pointer;
             border-bottom: 3px solid transparent; transition: all .15s; }}
    .tab:hover  {{ color: #fff; }}
    .tab.active {{ color: #fff; border-bottom-color: #fff; background: rgba(255,255,255,.1); }}

    /* ── Layout ── */
    .container {{ max-width: 1400px; margin: 0 auto; padding: 24px 32px; }}
    .tab-content {{ display: none; }}
    .tab-content.active {{ display: block; }}
    .section-title {{ font-size: .78rem; font-weight: 700; text-transform: uppercase;
                       letter-spacing: .07em; color: #94a3b8; margin: 28px 0 12px; }}

    /* ── Stat cards ── */
    .stats {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 14px; margin-bottom: 14px; }}
    .card {{ background: #fff; border-radius: 10px; padding: 18px 20px;
              box-shadow: 0 1px 3px rgba(0,0,0,.08); text-decoration: none; color: inherit; display: block; }}
    .card .value {{ font-size: 1.75rem; font-weight: 800; color: #2563eb; line-height: 1; }}
    .card .label {{ font-size: .7rem; text-transform: uppercase; letter-spacing: .06em;
                    color: #94a3b8; margin-top: 5px; }}
    .card.c-green .value {{ color: #16a34a; }}
    .card.c-blue  .value {{ color: #0891b2; }}
    .card-clickable {{ cursor: pointer; transition: box-shadow .15s, transform .1s; }}
    .card-clickable:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.12); transform: translateY(-1px); }}
    .card-link {{ cursor: pointer; transition: box-shadow .15s; }}
    .card-link:hover {{ box-shadow: 0 4px 12px rgba(0,0,0,.12); }}

    /* ── Expand panels ── */
    .expand-panel {{ background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
                     margin-bottom: 14px; overflow: hidden; border: 1px solid #e2e8f0; }}

    /* ── WoW box ── */
    .wow-box {{ display: grid; grid-template-columns: 1fr 1px 1fr;
                background: #fff; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,.08);
                margin-bottom: 28px; overflow: hidden;
                border: 1px solid #e2e8f0; }}
    .wow-col {{ padding: 20px 24px; }}
    .wow-divider {{ background: #e2e8f0; }}
    .wow-section-title {{ font-size: .85rem; font-weight: 700; margin-bottom: 12px;
                           display: flex; align-items: center; gap: 8px; }}
    .wow-count {{ font-size: 1.5rem; font-weight: 800; line-height: 1; }}
    .wow-list {{ list-style: none; display: flex; flex-direction: column; gap: 6px; }}
    .wow-list li {{ font-size: .85rem; font-weight: 500; }}

    /* ── Table wrapper ── */
    .table-wrap {{ background: #fff; border-radius: 10px;
                   box-shadow: 0 1px 3px rgba(0,0,0,.08); overflow-x: auto; }}
    .table-title {{ padding: 13px 18px; font-size: .82rem; font-weight: 600; color: #475569;
                    border-bottom: 1px solid #e2e8f0; background: #f8fafc;
                    display: flex; justify-content: space-between; align-items: center; }}
    .table-title .legend {{ display: flex; gap: 16px; font-weight: 400; font-size: .75rem; color: #64748b; }}
    .legend-dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 4px; }}

    table {{ width: 100%; border-collapse: collapse; }}
    th {{ padding: 9px 13px; text-align: left; font-size: .68rem; text-transform: uppercase;
           letter-spacing: .06em; color: #94a3b8; background: #f8fafc;
           border-bottom: 1px solid #e2e8f0; white-space: nowrap; }}
    td {{ padding: 9px 13px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }}
    tr:last-child td {{ border-bottom: none; }}
    tr:hover td {{ background: rgba(0,0,0,.012); }}

    .kw-cell  {{ font-weight: 500; min-width: 200px; }}
    .pos-cell {{ font-size: .95rem; font-weight: 700; min-width: 90px; }}
    .pos-num  {{ color: #1e293b; }}
    .pos-none {{ font-size: .75rem; color: #cbd5e1; font-weight: 400; }}

    .row-none {{ opacity: .6; }}

    .comp-cell {{ text-align: center; min-width: 70px; font-size: .82rem; }}
    .comp-none {{ color: #cbd5e1; }}

    .hist-header {{ text-align: center; }}
    .hist-cell   {{ text-align: center; color: #64748b; font-size: .75rem; min-width: 52px; }}
    .hist-top10  {{ color: #16a34a; font-weight: 700; }}
    .hist-top30  {{ color: #2563eb; font-weight: 600; }}
    .hist-none   {{ color: #e2e8f0; }}

    @media (max-width: 900px) {{
      .stats {{ grid-template-columns: repeat(2, 1fr); }}
      .wow-box {{ grid-template-columns: 1fr; }}
      .wow-divider {{ height: 1px; width: 100%; }}
      .container {{ padding: 16px; }}
      .tab-bar {{ padding: 0 16px; }}
    }}
  </style>
</head>
<body>

<header class="header">
  <div>
    <h1>Chrome Extension Keyword Tracker</h1>
    <p>{ext_count} extension(s) tracked · Chrome Web Store</p>
  </div>
  <div class="updated">Last updated<br>{last_updated}</div>
</header>

<nav class="tab-bar">
  {tabs_html}
</nav>

<div class="container">
  {content_html}
</div>

<script>
function togglePanel(id) {{
  const el = document.getElementById(id);
  if (el) el.style.display = el.style.display === 'none' ? '' : 'none';
}}

function showTab(idx, btn) {{
  document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(el => el.classList.remove('active'));
  document.getElementById('tab-' + idx).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body>
</html>"""

    with open(DASHBOARD_FILE, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Dashboard saved → {DASHBOARD_FILE}")


# ---------------------------------------------------------------------------
# SLACK NOTIFICATION
# ---------------------------------------------------------------------------

def send_slack(data, all_changes, config):
    slack_cfg = config.get("slack", {})
    if not slack_cfg.get("enabled"):
        return
    webhook_url = slack_cfg.get("webhook_url", "")
    if not webhook_url or "YOUR/WEBHOOK" in webhook_url:
        print("[SLACK] Skipped — no valid webhook URL.")
        return

    dates      = sorted(data.keys())
    today      = dates[-1] if dates else None
    yesterday  = dates[-2] if len(dates) >= 2 else None
    extensions = config["extensions"]

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "📊 Daily CWS Keyword Report"}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": f"*{datetime.utcnow().strftime('%B %d, %Y')}*  ·  Chrome Web Store  ·  {len(extensions)} extension(s)"}]},
        {"type": "divider"},
    ]

    for ext_cfg in extensions:
        ext_id      = ext_cfg["id"]
        ext_name    = ext_cfg.get("name", ext_id)
        keywords    = ext_cfg.get("keywords", [])
        comp_ids    = [c["id"] for c in ext_cfg.get("competitors", [])]

        our_raw   = data[today].get(ext_id, {}) if today else {}
        our_kws   = keyword_positions(our_raw)
        our_users = our_raw.get("_users")

        prev_raw   = data[yesterday].get(ext_id, {}) if yesterday else {}
        prev_users = prev_raw.get("_users")

        ranking = [v for v in our_kws.values() if isinstance(v, int)]
        top10   = sum(1 for v in ranking if v <= 10)
        top3    = sum(1 for v in ranking if v <= 3)

        # WoW top10
        lw_date_s  = find_last_week_date(dates, today) if today else None
        lw_kws_s   = keyword_positions(data[lw_date_s].get(ext_id, {})) if lw_date_s else {}
        lw_top10_s = sum(1 for v in lw_kws_s.values() if isinstance(v, int) and v <= 10)
        if lw_date_s and isinstance(lw_top10_s, int):
            d = top10 - lw_top10_s
            wow_top10_slack = f"  ({'▲+' if d > 0 else ('▼' if d < 0 else '→')}{abs(d) if d != 0 else ''} WoW)"
        else:
            wow_top10_slack = ""

        changes  = all_changes.get(ext_id, [])
        declined = [c for c in changes if c["current"] > c["prev"]]
        improved = [c for c in changes if c["current"] < c["prev"]]

        # Users delta
        if isinstance(our_users, int) and isinstance(prev_users, int) and our_users != prev_users:
            delta    = our_users - prev_users
            users_str = f"{format_users(our_users)}  ({'▲' if delta > 0 else '▼'} {format_users(abs(delta))})"
        else:
            users_str = format_users(our_users)

        # Competitor wins/losses
        win_kws = lose_kws = []
        if today:
            win_kws  = []
            lose_kws = []
            for kw in keywords:
                our = our_kws.get(kw)
                if not isinstance(our, int):
                    continue
                comp_pos = [
                    keyword_positions(data[today].get(c, {})).get(kw)
                    for c in comp_ids
                    if isinstance(keyword_positions(data[today].get(c, {})).get(kw), int)
                ]
                if not comp_pos:
                    continue
                best = min(comp_pos)
                if our < best:
                    win_kws.append((kw, our, best))
                elif our > best:
                    lose_kws.append((kw, our, best))

        ext_blocks = [
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*{ext_name}*\n`{ext_id[:20]}...`"},
                {"type": "mrkdwn", "text": f"*Users:*\n{users_str}"},
                {"type": "mrkdwn", "text": f"*Ranking:*\n{len(ranking)}/{len(keywords)} keywords"},
                {"type": "mrkdwn", "text": f"*Top 10 / Top 3:*\n{top10}{wow_top10_slack} / {top3} keywords"},
                {"type": "mrkdwn", "text": f"*Changes:*\n↑ {len(improved)} improved · ↓ {len(declined)} declined"},
                {"type": "mrkdwn", "text": f"*vs Competitors:*\n🏆 {len(win_kws)} winning · ⚠️ {len(lose_kws)} behind"},
            ]},
        ]

        if declined:
            mention = slack_cfg.get("mention_on_decline", "")
            prefix  = f"{mention} " if mention and len(declined) >= 3 else ""
            lines   = [f"• *{c['keyword']}*  #{c['prev']} → #{c['current']} _(↓{c['current']-c['prev']})_" for c in declined]
            ext_blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"{prefix}*📉 Declined ({len(declined)})*\n" + "\n".join(lines)}})

        if improved:
            lines = [f"• *{c['keyword']}*  #{c['prev']} → #{c['current']} _(↑{c['prev']-c['current']})_" for c in improved]
            ext_blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"*📈 Improved ({len(improved)})*\n" + "\n".join(lines)}})

        if lose_kws:
            lines = [f"• *{kw}*  — You #{our}, best competitor #{best}" for kw, our, best in lose_kws[:5]]
            ext_blocks.append({"type": "section",
                "text": {"type": "mrkdwn", "text": f"*⚠️ Behind a competitor*\n" + "\n".join(lines)}})

        blocks += ext_blocks
        blocks.append({"type": "divider"})

    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "View full dashboard → https://safwana-wy.github.io/chrome-extension-keyword-tracker/"}]})

    payload = {"blocks": blocks}
    if slack_cfg.get("channel"):
        payload["channel"] = slack_cfg["channel"]

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        if resp.status_code == 200 and resp.text == "ok":
            print("Slack notification sent.")
        else:
            print(f"[SLACK ERROR] {resp.status_code}: {resp.text}")
    except Exception as exc:
        print(f"[SLACK ERROR] {exc}")


# ---------------------------------------------------------------------------
# EMAIL ALERT
# ---------------------------------------------------------------------------

def send_email(all_changes, config):
    email_cfg = config.get("email", {})
    if not email_cfg.get("enabled"):
        return
    any_changes = any(v for v in all_changes.values())
    if not any_changes:
        return

    lines = []
    for ext_cfg in config["extensions"]:
        ext_id   = ext_cfg["id"]
        ext_name = ext_cfg.get("name", ext_id)
        changes  = all_changes.get(ext_id, [])
        if not changes:
            continue
        declined = [c for c in changes if c["current"] > c["prev"]]
        improved = [c for c in changes if c["current"] < c["prev"]]
        lines.append(f"<h3>{ext_name}</h3>")
        if declined:
            lines.append("<p style='color:#dc2626'>Declined: " +
                ", ".join(f"{c['keyword']} (#{c['prev']}→#{c['current']})" for c in declined) + "</p>")
        if improved:
            lines.append("<p style='color:#16a34a'>Improved: " +
                ", ".join(f"{c['keyword']} (#{c['prev']}→#{c['current']})" for c in improved) + "</p>")

    subject = f"[CWS Keyword Tracker] Position changes · {datetime.utcnow().strftime('%Y-%m-%d')}"
    body    = f"<html><body style='font-family:sans-serif;max-width:600px;margin:0 auto'>" \
              f"<h2>Keyword Position Changes</h2>{''.join(lines)}</body></html>"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = email_cfg["from"]
        msg["To"]      = email_cfg["to"]
        msg.attach(MIMEText(body, "html"))
        with smtplib.SMTP_SSL(email_cfg["smtp_host"], email_cfg["smtp_port"]) as server:
            server.login(email_cfg["username"], email_cfg["password"])
            server.sendmail(email_cfg["from"], email_cfg["to"], msg.as_string())
        print(f"Email sent to {email_cfg['to']}")
    except Exception as exc:
        print(f"[EMAIL ERROR] {exc}")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    dry_run = "--dry" in sys.argv
    config  = load_config()

    if dry_run:
        print("Dry run — regenerating dashboard from existing data.")
        data = load_data()
        if not data:
            print("No data found. Run without --dry first.")
            sys.exit(1)
        generate_dashboard(data, config)
    else:
        data, all_changes = run_check(config)
        generate_dashboard(data, config)
        send_slack(data, all_changes, config)
        send_email(all_changes, config)

    print("\nDone.")
