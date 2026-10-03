#!/usr/bin/env python3
"""
Buffalo Commute Hazard Outlook
------------------------------
Pulls the National Weather Service (NWS) forecast for downtown Buffalo and the
surrounding towns, rates each weekday commute window for winter hazards, and
writes the dashboard files (_dashboard.html, _method.html) the project page shows.

    python3 update.py          live data from api.weather.gov
    python3 update.py --demo   made-up storm, to preview the layout

Uses only the Python standard library.
"""
import html
import json
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# SETTINGS: everything you might want to tune lives here
# ----------------------------------------------------------------------------
TZ = ZoneInfo("America/New_York")
USER_AGENT = "buffalo-commute-hazard (england.dsa@gmail.com)"  # NWS asks for a contact

# Towns to rate, listed south to north: name -> (latitude, longitude)
TOWNS = {
    "Hamburg": (42.7159, -78.8295),
    "Orchard Park": (42.7675, -78.7439),
    "East Aurora": (42.7678, -78.6134),
    "Lackawanna": (42.8256, -78.8234),
    "West Seneca": (42.8501, -78.7998),
    "Downtown Buffalo": (42.8864, -78.8784),
    "Cheektowaga": (42.9034, -78.7548),
    "Lancaster": (42.9006, -78.6703),
    "Amherst": (42.9784, -78.7998),
    "Clarence": (42.9767, -78.5920),
    "Tonawanda": (43.0203, -78.8803),
    "Grand Island": (43.0334, -78.9628),
}

# Commute windows in local time: (label, short label, start hour, end hour)
WINDOWS = [("Morning", "AM", 6, 9), ("Evening", "PM", 16, 19)]
WORKDAYS_SHOWN = 5

LEVELS = ["Low", "Elevated", "High", "Severe"]

# Thresholds: (value, level reached). Levels: 1 Elevated, 2 High, 3 Severe.
SNOW_IN = [(0.5, 1), (2.0, 2), (4.0, 3)]      # inches falling during the window
ICE_IN = [(0.01, 2), (0.10, 3)]               # inches of ice during the window
GUST_MPH = [(35, 1), (50, 2)]                 # peak wind gust
FEELS_F = [(0, 1), (-15, 2)]                  # lowest "feels like" temperature (at or below)
VISIBILITY_MI = [(0.5, 1), (0.25, 2)]         # lowest visibility (at or below)
BLOWING_SNOW_GUST = 35                        # snow plus gusts this strong bumps the rating one level

# Words in the NWS hourly forecast that set a minimum rating
NWS_WORDING = {
    "Blizzard": 3,
    "Heavy Snow": 2,
    "Freezing Rain": 2,
    "Freezing Drizzle": 2,
    "Sleet": 1,
    "Wintry Mix": 1,
    "Blowing Snow": 1,
}
UNCERTAIN = ("Slight Chance", "Chance")       # forecasts starting with these count one level lower

# Official NWS alerts set a minimum rating while they are in effect
ALERT_LEVELS = {
    "Blizzard Warning": 3,
    "Ice Storm Warning": 3,
    "Snow Squall Warning": 3,
    "Winter Storm Warning": 2,
    "Lake Effect Snow Warning": 2,
    "Extreme Cold Warning": 2,
    "Winter Weather Advisory": 1,
    "Cold Weather Advisory": 1,
    "Winter Storm Watch": 1,
}

HERE = Path(__file__).parent
OUT = HERE / "_dashboard.html"
OUT_METHOD = HERE / "_method.html"


# ----------------------------------------------------------------------------
# 1. GET THE DATA
# ----------------------------------------------------------------------------
_cache = {}


def get_json(url):
    """Fetch a URL from the NWS API, retrying a few times (it occasionally hiccups)."""
    if url in _cache:
        return _cache[url]
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                _cache[url] = json.load(resp)
                return _cache[url]
        except Exception as err:  # noqa: BLE001
            last = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}: {last}")


def fetch_live():
    """For each town: the NWS gridded forecast, hourly forecast and active alerts."""
    data = {}
    for name, (lat, lon) in TOWNS.items():
        point = get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")["properties"]
        alerts = get_json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}")
        data[name] = {
            "grid": get_json(point["forecastGridData"])["properties"],
            "hourly": get_json(point["forecastHourly"])["properties"]["periods"],
            "alerts": [f["properties"] for f in alerts.get("features", [])],
        }
    return data


# ----------------------------------------------------------------------------
# 2. TURN THE FORECAST INTO HOURLY VALUES
# ----------------------------------------------------------------------------
DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def duration_hours(text):
    days, hours, _minutes = DURATION.fullmatch(text).groups()
    return max(1, int(days or 0) * 24 + int(hours or 0))


def convert(value, unit):
    """Convert NWS metric units to inches, mph, miles and Fahrenheit."""
    unit = (unit or "").split(":")[-1]
    if unit == "mm":
        return value / 25.4
    if unit == "km_h-1":
        return value * 0.621371
    if unit == "m_s-1":
        return value * 2.23694
    if unit == "degC":
        return value * 9 / 5 + 32
    if unit == "m":
        return value / 1609.34
    return value


def hourly(grid, layer, accumulates):
    """
    Expand an NWS forecast layer into {hour (UTC): value}.
    Each NWS value covers a time span such as "2026-01-12T12:00:00+00:00/PT6H".
    Totals (snow, ice) are spread evenly over the span; other values repeat.
    """
    data = grid.get(layer) or {}
    out = {}
    for item in data.get("values", []):
        if item.get("value") is None:
            continue
        start_text, span_text = item["validTime"].split("/")
        start = datetime.fromisoformat(start_text).astimezone(timezone.utc)
        span = duration_hours(span_text)
        value = convert(item["value"], data.get("uom"))
        if accumulates:
            value /= span
        for h in range(span):
            out[start + timedelta(hours=h)] = value
    return out


def build_series(town):
    grid = town["grid"]
    words = {}
    for period in town.get("hourly") or []:
        start = datetime.fromisoformat(period["startTime"]).astimezone(timezone.utc)
        words[start.replace(minute=0, second=0, microsecond=0)] = period.get("shortForecast") or ""
    return {
        "snow": hourly(grid, "snowfallAmount", True),
        "ice": hourly(grid, "iceAccumulation", True),
        "gust": hourly(grid, "windGust", False),
        "feels": hourly(grid, "apparentTemperature", False),
        "vis": hourly(grid, "visibility", False),
        "words": words,
    }


# ----------------------------------------------------------------------------
# 3. RATE EACH COMMUTE WINDOW
# ----------------------------------------------------------------------------
def commute_windows(now):
    """Upcoming weekday commute windows that have not finished yet."""
    windows, day, days_found = [], now.date(), 0
    while days_found < WORKDAYS_SHOWN:
        if day.weekday() < 5:
            todays = []
            for label, short, start_h, end_h in WINDOWS:
                start = datetime(day.year, day.month, day.day, start_h, tzinfo=TZ)
                end = datetime(day.year, day.month, day.day, end_h, tzinfo=TZ)
                if end > now:
                    todays.append({"day": day, "label": label, "short": short, "start": start, "end": end})
            if todays:
                windows.extend(todays)
                days_found += 1
        day += timedelta(days=1)
    return windows


def window_hours(window):
    start = window["start"].astimezone(timezone.utc)
    count = int((window["end"] - window["start"]).total_seconds() // 3600)
    return [start + timedelta(hours=h) for h in range(count)]


def measure(series, hours, how):
    values = [series[h] for h in hours if h in series]
    return how(values) if values else None


def level_at_or_above(value, table):
    return max([lvl for threshold, lvl in table if value >= threshold], default=0)


def level_at_or_below(value, table):
    return max([lvl for threshold, lvl in table if value <= threshold], default=0)


def wording_level(phrase):
    """Minimum rating implied by an NWS forecast phrase such as 'Chance Freezing Rain'."""
    level = max([lvl for words, lvl in NWS_WORDING.items() if words.lower() in phrase.lower()], default=0)
    if level and phrase.startswith(UNCERTAIN):
        level -= 1
    return level


def rate(metrics, phrases, alert_events):
    """Return (level or None, list of reasons). The level is the worst single factor."""
    snow, ice, gust, feels, vis = (metrics[k] for k in ("snow", "ice", "gust", "feels", "vis"))
    if all(v is None for v in (snow, ice, gust, feels, vis)) and not phrases and not alert_events:
        return None, []
    found = []
    snow_level = level_at_or_above(snow, SNOW_IN) if snow is not None else 0
    if snow_level:
        found.append((snow_level, f"{snow:.1f} in snow"))
    if ice is not None and level_at_or_above(ice, ICE_IN):
        found.append((level_at_or_above(ice, ICE_IN), f"{ice:.2f} in ice"))
    if gust is not None and level_at_or_above(gust, GUST_MPH):
        found.append((level_at_or_above(gust, GUST_MPH), f"gusts {gust:.0f} mph"))
    if feels is not None and level_at_or_below(feels, FEELS_F):
        found.append((level_at_or_below(feels, FEELS_F), f"feels like {feels:.0f}°F"))
    if vis is not None and level_at_or_below(vis, VISIBILITY_MI):
        found.append((level_at_or_below(vis, VISIBILITY_MI), f"visibility {vis:.2g} mi"))
    for phrase in phrases:
        if wording_level(phrase):
            found.append((wording_level(phrase), f"NWS forecast: {phrase}"))
    level = max([lvl for lvl, _ in found], default=0)
    nws_blowing = any("blowing snow" in p.lower() for p in phrases)
    if snow_level and (nws_blowing or (gust is not None and gust >= BLOWING_SNOW_GUST)):
        level = min(3, level + 1)
        found.append((level, "blowing snow"))
    for event in alert_events:
        level = max(level, ALERT_LEVELS[event])
        found.append((ALERT_LEVELS[event], event))
    found.sort(key=lambda pair: -pair[0])
    return level, [text for _, text in found]


def alert_times(alert):
    start = alert.get("onset") or alert.get("effective")
    end = alert.get("ends") or alert.get("expires")
    if not start or not end:
        return None, None
    return datetime.fromisoformat(start), datetime.fromisoformat(end)


def assess(data, now):
    """One row per commute window, one cell per town."""
    series = {name: build_series(town) for name, town in data.items()}
    rows = []
    for window in commute_windows(now):
        hours = window_hours(window)
        cells = []
        for name in TOWNS:
            s = series[name]
            metrics = {
                "snow": measure(s["snow"], hours, sum),
                "ice": measure(s["ice"], hours, sum),
                "gust": measure(s["gust"], hours, max),
                "feels": measure(s["feels"], hours, min),
                "vis": measure(s["vis"], hours, min),
            }
            phrases = []
            for h in hours:
                phrase = s["words"].get(h)
                if phrase and phrase not in phrases:
                    phrases.append(phrase)
            events = set()
            for alert in data[name].get("alerts", []):
                start, end = alert_times(alert)
                if alert.get("event") in ALERT_LEVELS and start and start < window["end"] and end > window["start"]:
                    events.add(alert["event"])
            level, reasons = rate(metrics, phrases, sorted(events))
            cells.append({"town": name, "level": level, "reasons": reasons, "metrics": metrics,
                          "phrases": phrases, "events": sorted(events)})
        rows.append({**window, "cells": cells})
    return rows


# ----------------------------------------------------------------------------
# 4. DRAW THE DASHBOARD
# ----------------------------------------------------------------------------
COLORS = ["#0ca30c", "#fab219", "#ec835a", "#d03b3b"]
ICONS = ["✓", "!", "▲", "✕"]

STYLE = """
<style>
.cm { --ink:#14213d; --muted:#5b6577; --line:#dfe5ee; font-size:.95rem; color:var(--ink); }
.cm h3 { font-size:1.15rem; margin:1.8rem 0 .2rem; }
.cm-sub { color:var(--muted); font-size:.85rem; margin:0 0 .6rem; }
.cm-note { background:#f4f6fa; border:1px solid var(--line); border-radius:6px; padding:.6rem .9rem; margin-bottom:1rem; color:var(--muted); }
.cm-alert { border:1px solid var(--line); border-left:5px solid #d03b3b; border-radius:6px; padding:.7rem .9rem; margin-bottom:.6rem; background:#fff; }
.cm-alert b { font-size:1.02rem; }
.cm-alert p { margin:.25rem 0 0; }
.cm-alert .cm-meta { color:var(--muted); font-size:.85rem; }
.cm-hero { display:flex; gap:1.25rem; align-items:center; flex-wrap:wrap; border:1px solid var(--line); border-left:8px solid var(--c); border-radius:8px; padding:1.1rem 1.25rem; margin:1rem 0 .5rem; background:#fff; }
.cm-hero .cm-big { font-size:2.1rem; font-weight:700; line-height:1.1; }
.cm-hero .cm-when { color:var(--muted); font-size:.85rem; text-transform:uppercase; letter-spacing:.06em; }
.cm-hero .cm-why { flex:1 1 18rem; }
.cm-icon { display:inline-flex; align-items:center; justify-content:center; width:1.35em; height:1.35em; border-radius:50%; background:var(--c); color:#fff; font-size:.8em; font-weight:700; flex:none; }
.cm-hero .cm-icon { font-size:1.4rem; }
.cm-chip { display:inline-flex; align-items:center; gap:.4rem; border-left:4px solid var(--c); border-radius:4px; padding:.3rem .5rem; font-weight:600; background:color-mix(in srgb, var(--c) 13%, #fff); white-space:nowrap; }
.cm-none { --c:#c5ccd6; color:var(--muted); font-weight:400; }
.cm-day { font-weight:700; border-bottom:1px solid var(--line); padding:.9rem 0 .25rem; margin-bottom:.35rem; }
.cm-win { display:grid; grid-template-columns:8.5rem 1fr; gap:.2rem .75rem; padding:.3rem 0; }
.cm-win .cm-time small { display:block; color:var(--muted); }
.cm-group { display:grid; grid-template-columns:7.2rem 1fr; gap:.6rem; align-items:start; padding:.15rem 0; }
.cm-group .cm-chip { display:flex; }
.cm-towns { font-weight:600; }
.cm-detail { display:block; color:var(--muted); font-size:.84rem; line-height:1.4; }
.cm-scroll { overflow-x:auto; }
.cm table { width:100%; border-collapse:separate; border-spacing:3px; margin:0; }
.cm-grid table { min-width:820px; }
.cm th { text-align:left; font-weight:600; padding:.3rem .35rem; vertical-align:bottom; border:none; font-size:.82rem; }
.cm th small { display:block; color:var(--muted); font-weight:400; }
.cm td { padding:0; vertical-align:top; border:none; }
.cm-grid td.cm-town { font-weight:600; white-space:nowrap; padding:.3rem .5rem .3rem 0; }
.cm-grid .cm-chip { display:flex; font-size:.78rem; padding:.3rem .35rem; gap:.3rem; }
.cm-legend { display:flex; gap:1.2rem; flex-wrap:wrap; margin:1rem 0 .4rem; }
.cm-legend span { display:inline-flex; align-items:center; gap:.4rem; }
.cm-foot { color:var(--muted); font-size:.82rem; margin-top:.6rem; }
.cm-rules table { border-spacing:0; }
.cm-rules td, .cm-rules th { vertical-align:top; padding:.35rem .5rem; border-bottom:1px solid var(--line); font-size:.9rem; }
@media (max-width:600px) { .cm-win, .cm-group { grid-template-columns:1fr; } }
</style>
"""


def esc(text):
    return html.escape(str(text))


def chip(level, label=None):
    if level is None:
        return '<div class="cm-chip cm-none">No forecast</div>'
    return (f'<div class="cm-chip" style="--c:{COLORS[level]}">'
            f'<span class="cm-icon" aria-hidden="true">{ICONS[level]}</span>{label or LEVELS[level]}</div>')


def hours_text(window):
    def clock(dt):
        return dt.strftime("%I").lstrip("0") + (" a.m." if dt.hour < 12 else " p.m.")
    return f"{clock(window['start'])} to {clock(window['end'])}"


def tooltip(cell):
    m = cell["metrics"]

    def show(value, fmt):
        return fmt.format(value) if value is not None else "not yet forecast"
    text = (f"Snow: {show(m['snow'], '{:.1f} in')} | Ice: {show(m['ice'], '{:.2f} in')} | "
            f"Peak gust: {show(m['gust'], '{:.0f} mph')} | Feels like: {show(m['feels'], '{:.0f}°F')}")
    if cell["phrases"]:
        text += " | NWS forecast: " + ", then ".join(cell["phrases"][:3])
    if cell["events"]:
        text += " | " + ", ".join(cell["events"])
    return text


def group_detail(cells):
    """One line describing the towns that share a rating, leading with the NWS's own wording."""
    bits = []
    phrases = []
    for c in cells:
        for p in c["phrases"]:
            if p not in phrases:
                phrases.append(p)
    winter = [p for p in phrases if wording_level(p) or "snow" in p.lower()]
    if winter or phrases:
        bits.append("NWS forecast: " + ", ".join((winter or phrases)[:2]))
    many = len(cells) > 1

    def vals(key):
        return [c["metrics"][key] for c in cells if c["metrics"][key] is not None]
    if vals("snow") and max(vals("snow")) >= SNOW_IN[0][0]:
        bits.append(("up to " if many else "") + f"{max(vals('snow')):.1f} in snow")
    if vals("ice") and max(vals("ice")) >= ICE_IN[0][0]:
        bits.append(("up to " if many else "") + f"{max(vals('ice')):.2f} in ice")
    if vals("gust") and max(vals("gust")) >= GUST_MPH[0][0]:
        bits.append(f"gusts to {max(vals('gust')):.0f} mph")
    if vals("feels") and min(vals("feels")) <= FEELS_F[0][0]:
        bits.append(f"feels like {min(vals('feels')):.0f}°F")
    if vals("vis") and min(vals("vis")) <= VISIBILITY_MI[0][0]:
        bits.append(f"visibility down to {min(vals('vis')):.2g} mi")
    if any("blowing snow" in c["reasons"] for c in cells):
        bits.append("blowing snow")
    events = sorted({e for c in cells for e in c["events"]})
    bits.extend(events)
    if any(c["metrics"]["snow"] is None for c in cells):
        bits.append("snow amount not yet forecast")
    return " · ".join(bits)


def groups_by_level(cells):
    """[(level, [cells])] from most to least severe, leaving out Low unless everything is Low."""
    rated = [c for c in cells if c["level"] is not None]
    out = []
    for level in (3, 2, 1):
        members = [c for c in rated if c["level"] == level]
        if members:
            out.append((level, members))
    return out


def alert_section(text, name):
    """Pull a section such as WHAT or IMPACTS out of the NWS alert text."""
    match = re.search(rf"\*\s*{name}\.\.\.(.*?)(?=\n\s*\*|\n\n|\Z)", text or "", re.S)
    return " ".join(match.group(1).split()) if match else ""


def render_alerts(data):
    out, seen = [], set()
    for town in data.values():
        for alert in town.get("alerts", []):
            key = alert.get("id") or alert.get("headline")
            if key in seen or alert.get("event") not in ALERT_LEVELS:
                continue
            seen.add(key)
            what = alert_section(alert.get("description"), "WHAT")
            impacts = alert_section(alert.get("description"), "IMPACTS")
            body = f'<div class="cm-alert"><b>{esc(alert["event"])}</b>'
            body += f'<div class="cm-meta">{esc(alert.get("headline") or "")}</div>'
            if alert.get("areaDesc"):
                body += f'<div class="cm-meta">NWS forecast zones: {esc(alert["areaDesc"])}</div>'
            if what:
                body += f"<p><b>What:</b> {esc(what)}</p>"
            if impacts:
                body += f"<p><b>Impacts:</b> {esc(impacts)}</p>"
            out.append(body + "</div>")
    return out


def render(rows, data, now, demo=False, error=None):
    parts = ["```{=html}", STYLE, '<div class="cm">']
    if demo:
        parts.append('<div class="cm-note"><b>Sample data.</b> This is a made-up storm used to preview the layout, not a real forecast.</div>')
    if error:
        parts.append('<div class="cm-note"><b>The forecast could not be loaded at the last update.</b> '
                     'Check the <a href="https://www.weather.gov/buf/">National Weather Service Buffalo</a> forecast directly.</div>')
    parts.extend(render_alerts(data))

    # Headline: the next commute
    rated = [r for r in rows if any(c["level"] is not None for c in r["cells"])]
    if rated:
        nxt = rated[0]
        groups = groups_by_level(nxt["cells"])
        level = groups[0][0] if groups else 0
        if groups:
            towns = ", ".join(c["town"] for c in groups[0][1])
            why = (f'<div><b>Areas impacted:</b> {esc(towns)}</div>'
                   f'<span class="cm-detail">{esc(group_detail(groups[0][1]))}</span>')
        else:
            why = "No winter hazards expected in any area."
        parts.append(
            f'<div class="cm-hero" style="--c:{COLORS[level]}">'
            f'<span class="cm-icon" aria-hidden="true">{ICONS[level]}</span>'
            f'<div><div class="cm-when">Next commute · {nxt["day"].strftime("%A")} {nxt["label"].lower()}, {hours_text(nxt)}</div>'
            f'<div class="cm-big">{LEVELS[level]}</div></div>'
            f'<div class="cm-why">{why}</div></div>'
        )

    # Areas impacted, commute by commute
    parts.append('<h3>Areas impacted by commute</h3><p class="cm-sub">Towns are grouped by rating. Wording in each line comes from the National Weather Service forecast.</p>')
    last_day = None
    for row in rows:
        if row["day"] != last_day:
            last_day = row["day"]
            parts.append(f'<div class="cm-day">{row["day"].strftime("%A, %B %-d")}</div>')
        groups = groups_by_level(row["cells"])
        if groups:
            body = "".join(
                f'<div class="cm-group">{chip(level)}<div><span class="cm-towns">{esc(", ".join(c["town"] for c in members))}</span>'
                f'<span class="cm-detail">{esc(group_detail(members))}</span></div></div>'
                for level, members in groups
            )
            if any(c["level"] == 0 for c in row["cells"]):
                lows = ", ".join(c["town"] for c in row["cells"] if c["level"] == 0)
                body += f'<div class="cm-group">{chip(0)}<div><span class="cm-detail">{esc(lows)}</span></div></div>'
        elif any(c["level"] == 0 for c in row["cells"]):
            note = "snow amount not yet forecast" if any(c["metrics"]["snow"] is None for c in row["cells"]) else ""
            body = f'<div class="cm-group">{chip(0)}<div><span class="cm-towns">All areas</span><span class="cm-detail">{note}</span></div></div>'
        else:
            body = f'<div class="cm-group">{chip(None)}<div></div></div>'
        parts.append(f'<div class="cm-win"><div class="cm-time">{row["label"]}<small>{hours_text(row)}</small></div><div>{body}</div></div>')

    # Town by town grid
    parts.append('<h3>Town by town</h3><p class="cm-sub">Towns run south to north. Hover over a rating for the numbers behind it.</p>')
    head = "".join(f'<th>{r["day"].strftime("%a")} {r["short"]}<small>{r["day"].strftime("%b %-d")}</small></th>' for r in rows)
    parts.append(f'<div class="cm-grid cm-scroll"><table data-quarto-disable-processing="true"><thead><tr><th></th>{head}</tr></thead><tbody>')
    for i, name in enumerate(TOWNS):
        tds = "".join(f'<td title="{esc(tooltip(r["cells"][i]))}">{chip(r["cells"][i]["level"])}</td>' for r in rows)
        parts.append(f'<tr><td class="cm-town">{esc(name)}</td>{tds}</tr>')
    parts.append("</tbody></table></div>")

    legend = "".join(
        f'<span><span class="cm-icon" style="--c:{COLORS[i]}" aria-hidden="true">{ICONS[i]}</span>{LEVELS[i]}</span>'
        for i in range(4)
    )
    parts.append(f'<div class="cm-legend">{legend}</div>')
    parts.append(
        f'<div class="cm-foot">Updated {now.strftime("%A, %B %-d at %-I:%M %p")} Eastern. '
        'Forecasts, alerts and alert wording: <a href="https://www.weather.gov/buf/">National Weather Service Buffalo</a>. '
        'This is a personal project, not an official forecast. For travel bans and road closures, check Erie County and the City of Buffalo.</div>'
    )
    parts += ["</div>", "```", ""]
    return "\n".join(parts)


def render_method():
    """The thresholds table, built from the settings above so the page never drifts from the code."""
    def cells(table, fmt):
        by_level = {lvl: fmt.format(value) for value, lvl in table}
        return "".join(f"<td>{by_level.get(l, '')}</td>" for l in (1, 2, 3))

    def listed(mapping):
        return "".join("<td>" + "<br>".join(esc(k) for k, lvl in mapping.items() if lvl == l) + "</td>" for l in (1, 2, 3))
    body = (
        f"<tr><th>Snow during the window</th>{cells(SNOW_IN, '{} in or more')}</tr>"
        f"<tr><th>Ice during the window</th>{cells(ICE_IN, '{} in or more')}</tr>"
        f"<tr><th>Peak wind gust</th>{cells(GUST_MPH, '{} mph or more')}</tr>"
        f"<tr><th>Lowest “feels like”</th>{cells(FEELS_F, '{}°F or colder')}</tr>"
        f"<tr><th>Lowest visibility</th>{cells(VISIBILITY_MI, '{} mi or less')}</tr>"
        f"<tr><th>NWS hourly forecast says</th>{listed(NWS_WORDING)}</tr>"
        f"<tr><th>NWS alert in effect</th>{listed(ALERT_LEVELS)}</tr>"
    )
    return "\n".join([
        "```{=html}", STYLE, '<div class="cm">',
        '<p>Each town and commute window takes the rating of its worst single factor. '
        f'Snow combined with gusts of {BLOWING_SNOW_GUST} mph or more, or with blowing snow in the NWS forecast, is bumped up one level. '
        'An NWS forecast phrase that begins with “Chance” or “Slight Chance” counts one level lower.</p>'
        '<div class="cm-rules cm-scroll"><table data-quarto-disable-processing="true"><thead><tr><th>Factor</th>'
        + "".join(f"<th>{LEVELS[l]}</th>" for l in (1, 2, 3))
        + f"</tr></thead><tbody>{body}</tbody></table></div>",
        "</div>", "```", "",
    ])


# ----------------------------------------------------------------------------
# 5. SAMPLE STORM (for previewing the layout only)
# ----------------------------------------------------------------------------
def demo_data(now):
    """Made-up lake effect event, returned in the same shape the NWS API uses."""
    workdays = []
    for w in commute_windows(now):
        if w["day"] not in workdays:
            workdays.append(w["day"])
    band = {"Hamburg": 1.3, "Orchard Park": 1.6, "East Aurora": 1.1, "Lackawanna": 0.7, "West Seneca": 0.8,
            "Lancaster": 0.35, "Cheektowaga": 0.3, "Downtown Buffalo": 0.2}
    icing = ("Amherst", "Tonawanda", "Grand Island", "Clarence", "Downtown Buffalo")

    def conditions(town, local):
        idx = workdays.index(local.date()) if local.date() in workdays else -1
        h = local.hour
        snow, ice, gust, feels, words = 0.0, 0.0, 14.0, 27.0, "Mostly Cloudy"
        if idx == 0:
            gust, feels = 42.0, 3.0
            snow = band.get(town, 0.0) * (1.0 if h < 13 else 0.3 if h < 20 else 0.0)
            if snow >= 1.0:
                words = "Heavy Snow and Blowing Snow"
            elif snow >= 0.25:
                words = "Snow Showers and Blowing Snow"
            elif snow > 0:
                words = "Chance Snow Showers"
        elif idx == 1:
            gust, feels = 24.0, (-6.0 if h < 11 else 9.0)
            words = "Mostly Sunny" if h < 18 else "Partly Cloudy"
        elif idx == 2:
            feels = 30.0
            if 4 <= h < 10 and town in icing:
                ice, words = 0.012, "Freezing Rain Likely"
            elif 4 <= h < 10:
                words = "Chance Freezing Rain"
        return snow, ice, gust, feels, words

    start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % 6)
    data = {}
    for town in TOWNS:
        layers = {k: [] for k in ("snow", "ice", "gust", "feels")}
        periods = []
        for block in range(0, 24 * 8, 6):
            block_start = start + timedelta(hours=block)
            snow_total = ice_total = 0.0
            for h in range(6):
                t = block_start + timedelta(hours=h)
                snow, ice, gust, feels, words = conditions(town, t.astimezone(TZ))
                snow_total += snow
                ice_total += ice
                layers["gust"].append({"validTime": f"{t.isoformat()}/PT1H", "value": gust / 0.621371})
                layers["feels"].append({"validTime": f"{t.isoformat()}/PT1H", "value": (feels - 32) * 5 / 9})
                if block < 156:  # the real hourly forecast runs about six and a half days
                    periods.append({"startTime": t.astimezone(TZ).isoformat(), "shortForecast": words})
            if block < 132:  # like the real feed, snow and ice totals stop short of the full forecast
                layers["snow"].append({"validTime": f"{block_start.isoformat()}/PT6H", "value": snow_total * 25.4})
                layers["ice"].append({"validTime": f"{block_start.isoformat()}/PT6H", "value": ice_total * 25.4})
        data[town] = {
            "grid": {
                "snowfallAmount": {"uom": "wmoUnit:mm", "values": layers["snow"]},
                "iceAccumulation": {"uom": "wmoUnit:mm", "values": layers["ice"]},
                "windGust": {"uom": "wmoUnit:km_h-1", "values": layers["gust"]},
                "apparentTemperature": {"uom": "wmoUnit:degC", "values": layers["feels"]},
            },
            "hourly": periods,
            "alerts": [],
        }
    if workdays:
        d = workdays[0]
        sample = {
            "id": "demo-1", "event": "Lake Effect Snow Warning",
            "headline": "Sample alert: Lake Effect Snow Warning in effect until 7 p.m.",
            "areaDesc": "Southern Erie",
            "description": "* WHAT...Heavy lake effect snow. Additional snow accumulations of 8 to 14 inches in the most persistent lake snows. Winds gusting as high as 45 mph.\n\n"
                           "* WHERE...Southern Erie county.\n\n* WHEN...Until 7 PM.\n\n"
                           "* IMPACTS...Travel will be very difficult with deep snow cover on roads and very poor visibility. The hazardous conditions will impact the morning and evening commutes.",
            "onset": datetime(d.year, d.month, d.day, 1, tzinfo=TZ).isoformat(),
            "ends": datetime(d.year, d.month, d.day, 19, tzinfo=TZ).isoformat(),
        }
        for town in ("Hamburg", "Orchard Park", "East Aurora"):
            data[town]["alerts"].append(sample)
    return data


def main():
    demo = "--demo" in sys.argv
    now = datetime.now(TZ)
    try:
        data = demo_data(now) if demo else fetch_live()
        page = render(assess(data, now), data, now, demo=demo)
    except Exception as err:  # noqa: BLE001  (never break the site build over a bad fetch)
        print(f"WARNING: {err}", file=sys.stderr)
        page = render([], {}, now, error=str(err))
    OUT.write_text(page, encoding="utf-8")
    OUT_METHOD.write_text(render_method(), encoding="utf-8")
    print(f"Wrote {OUT.name} and {OUT_METHOD.name}")


if __name__ == "__main__":
    main()
