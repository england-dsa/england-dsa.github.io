#!/usr/bin/env python3
"""
Buffalo Commute Hazard Outlook
------------------------------
Pulls the National Weather Service forecast for downtown Buffalo and three
commuter corridors, rates each weekday commute window for winter hazards,
and writes the dashboard files (_dashboard.html, _method.html) that the project page displays.

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

# Forecast points (latitude, longitude)
POINTS = {
    "Downtown Buffalo": (42.8864, -78.8784),
    "Orchard Park": (42.7675, -78.7439),
    "Amherst": (42.9784, -78.7998),
    "Lancaster": (42.9006, -78.6703),
}

# A corridor is rated on the worst conditions at any of its points
CORRIDORS = [
    ("Downtown core", "Walking and transit", ["Downtown Buffalo"]),
    ("Southtowns", "Orchard Park to downtown", ["Orchard Park", "Downtown Buffalo"]),
    ("Northtowns", "Amherst to downtown", ["Amherst", "Downtown Buffalo"]),
    ("East suburbs", "Lancaster to downtown", ["Lancaster", "Downtown Buffalo"]),
]

# Commute windows in local time: (label, start hour, end hour)
WINDOWS = [("Morning", 6, 9), ("Evening", 16, 19)]
WORKDAYS_SHOWN = 5

LEVELS = ["Low", "Elevated", "High", "Severe"]

# Thresholds: (value, level reached). Levels: 1 Elevated, 2 High, 3 Severe.
SNOW_IN = [(0.5, 1), (2.0, 2), (4.0, 3)]      # inches falling during the window
ICE_IN = [(0.01, 2), (0.10, 3)]               # inches of ice during the window
GUST_MPH = [(35, 1), (50, 2)]                 # peak wind gust
FEELS_F = [(0, 1), (-15, 2)]                  # lowest "feels like" temperature (at or below)
BLOWING_SNOW_GUST = 35                        # snow plus gusts this strong bumps the rating one level

# Official alerts set a minimum rating while they are in effect
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
def get_json(url):
    """Fetch a URL from the NWS API, retrying a few times (it occasionally hiccups)."""
    last = None
    for attempt in range(4):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception as err:  # noqa: BLE001
            last = err
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}: {last}")


def fetch_live():
    """Return ({point: grid properties}, {point: [alert properties]})."""
    grids, alerts = {}, {}
    for name, (lat, lon) in POINTS.items():
        point = get_json(f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}")
        grids[name] = get_json(point["properties"]["forecastGridData"])["properties"]
        found = get_json(f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}")
        alerts[name] = [f["properties"] for f in found.get("features", [])]
    return grids, alerts


# ----------------------------------------------------------------------------
# 2. TURN THE FORECAST INTO HOURLY VALUES
# ----------------------------------------------------------------------------
DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?")


def duration_hours(text):
    days, hours, _minutes = DURATION.fullmatch(text).groups()
    return max(1, int(days or 0) * 24 + int(hours or 0))


def convert(value, unit):
    """Convert NWS metric units to inches, mph and Fahrenheit."""
    unit = (unit or "").split(":")[-1]
    if unit == "mm":
        return value / 25.4
    if unit == "km_h-1":
        return value * 0.621371
    if unit == "m_s-1":
        return value * 2.23694
    if unit == "degC":
        return value * 9 / 5 + 32
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


def build_hourly(grids):
    return {
        name: {
            "snow": hourly(grid, "snowfallAmount", True),
            "ice": hourly(grid, "iceAccumulation", True),
            "gust": hourly(grid, "windGust", False),
            "feels": hourly(grid, "apparentTemperature", False),
        }
        for name, grid in grids.items()
    }


# ----------------------------------------------------------------------------
# 3. RATE EACH COMMUTE WINDOW
# ----------------------------------------------------------------------------
def commute_windows(now):
    """Upcoming weekday commute windows that have not finished yet."""
    windows, day = [], now.date()
    days_found = 0
    while days_found < WORKDAYS_SHOWN:
        if day.weekday() < 5:
            todays = []
            for label, start_h, end_h in WINDOWS:
                start = datetime(day.year, day.month, day.day, start_h, tzinfo=TZ)
                end = datetime(day.year, day.month, day.day, end_h, tzinfo=TZ)
                if end > now:
                    todays.append({"day": day, "label": label, "start": start, "end": end})
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


def rate(metrics, alert_events):
    """Return (level or None, list of reasons). The level is the worst single factor."""
    snow, ice, gust, feels = (metrics[k] for k in ("snow", "ice", "gust", "feels"))
    if all(v is None for v in (snow, ice, gust, feels)) and not alert_events:
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
    level = max([lvl for lvl, _ in found], default=0)
    if snow_level and gust is not None and gust >= BLOWING_SNOW_GUST:
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


def assess(hourly_data, alerts, now):
    """Build the full table: one row per window, one cell per corridor."""
    rows = []
    for window in commute_windows(now):
        hours = window_hours(window)
        cells = []
        for name, note, points in CORRIDORS:
            def worst(key, how_point, how_corridor):
                vals = [measure(hourly_data[p][key], hours, how_point) for p in points]
                vals = [v for v in vals if v is not None]
                return how_corridor(vals) if vals else None

            metrics = {
                "snow": worst("snow", sum, max),
                "ice": worst("ice", sum, max),
                "gust": worst("gust", max, max),
                "feels": worst("feels", min, min),
            }
            events = set()
            for p in points:
                for alert in alerts.get(p, []):
                    start, end = alert_times(alert)
                    if alert.get("event") in ALERT_LEVELS and start and start < window["end"] and end > window["start"]:
                        events.add(alert["event"])
            level, reasons = rate(metrics, sorted(events))
            cells.append({"corridor": name, "level": level, "reasons": reasons, "metrics": metrics})
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
.cm-note { background:#f4f6fa; border:1px solid var(--line); border-radius:6px; padding:.6rem .9rem; margin-bottom:1rem; color:var(--muted); }
.cm-alert { border:1px solid var(--line); border-left:5px solid #d03b3b; border-radius:6px; padding:.6rem .9rem; margin-bottom:.6rem; background:#fff; }
.cm-alert b { display:block; }
.cm-hero { display:flex; gap:1.25rem; align-items:center; flex-wrap:wrap; border:1px solid var(--line); border-left:8px solid var(--c); border-radius:8px; padding:1.1rem 1.25rem; margin:1rem 0 1.5rem; background:#fff; }
.cm-hero .cm-big { font-size:2.1rem; font-weight:700; line-height:1.1; }
.cm-hero .cm-when { color:var(--muted); font-size:.85rem; text-transform:uppercase; letter-spacing:.06em; }
.cm-hero .cm-why { flex:1 1 16rem; }
.cm-icon { display:inline-flex; align-items:center; justify-content:center; width:1.35em; height:1.35em; border-radius:50%; background:var(--c); color:#fff; font-size:.8em; font-weight:700; flex:none; }
.cm-hero .cm-icon { font-size:1.4rem; }
.cm-scroll { overflow-x:auto; }
.cm table { width:100%; border-collapse:separate; border-spacing:0 4px; min-width:640px; margin:0; }
.cm th { text-align:left; font-weight:600; padding:.35rem .5rem; vertical-align:bottom; border:none; }
.cm th small, .cm td small { display:block; color:var(--muted); font-weight:400; }
.cm td { padding:.25rem .3rem; vertical-align:top; border:none; }
.cm .cm-day td { padding-top:.8rem; font-weight:700; border-bottom:1px solid var(--line); }
.cm .cm-win { white-space:nowrap; padding-left:.5rem; }
.cm-chip { display:flex; align-items:center; gap:.45rem; border-left:4px solid var(--c); border-radius:4px; padding:.35rem .5rem; font-weight:600; background:color-mix(in srgb, var(--c) 13%, #fff); }
.cm-cell small { padding:.15rem 0 0 .6rem; font-size:.78rem; line-height:1.3; }
.cm-none { --c:#c5ccd6; color:var(--muted); }
.cm-legend { display:flex; gap:1.2rem; flex-wrap:wrap; margin:1rem 0 .4rem; }
.cm-legend span { display:inline-flex; align-items:center; gap:.4rem; }
.cm-foot { color:var(--muted); font-size:.82rem; margin-top:.6rem; }
.cm-rules td, .cm-rules th { vertical-align:top; padding:.35rem .5rem; border-bottom:1px solid var(--line); }
.cm-rules table { border-spacing:0; min-width:0; }
</style>
"""


def esc(text):
    return html.escape(str(text))


def chip(level):
    if level is None:
        return '<div class="cm-chip cm-none">No forecast yet</div>'
    return (f'<div class="cm-chip" style="--c:{COLORS[level]}">'
            f'<span class="cm-icon" aria-hidden="true">{ICONS[level]}</span>{LEVELS[level]}</div>')


def tooltip(metrics):
    def show(value, fmt, missing="not yet forecast"):
        return fmt.format(value) if value is not None else missing
    return (f"Snow: {show(metrics['snow'], '{:.1f} in')} | Ice: {show(metrics['ice'], '{:.2f} in')} | "
            f"Peak gust: {show(metrics['gust'], '{:.0f} mph')} | Feels like: {show(metrics['feels'], '{:.0f}°F')}")


def hours_text(window):
    def clock(dt):
        return dt.strftime("%I").lstrip("0") + (" a.m." if dt.hour < 12 else " p.m.")
    return f"{clock(window['start'])} to {clock(window['end'])}"


def threshold_rows():
    def cells(table, fmt):
        by_level = {lvl: fmt.format(value) for value, lvl in table}
        return "".join(f"<td>{by_level.get(l, '')}</td>" for l in (1, 2, 3))
    alert_cells = "".join(
        "<td>" + "<br>".join(esc(e) for e, lvl in ALERT_LEVELS.items() if lvl == l) + "</td>" for l in (1, 2, 3)
    )
    return (
        f"<tr><th>Snow during the window</th>{cells(SNOW_IN, '{} in or more')}</tr>"
        f"<tr><th>Ice during the window</th>{cells(ICE_IN, '{} in or more')}</tr>"
        f"<tr><th>Peak wind gust</th>{cells(GUST_MPH, '{} mph or more')}</tr>"
        f"<tr><th>Lowest “feels like”</th>{cells(FEELS_F, '{}°F or colder')}</tr>"
        f"<tr><th>Official alert in effect</th>{alert_cells}</tr>"
    )


def render(rows, alerts, now, demo=False, error=None):
    parts = ["```{=html}", STYLE, '<div class="cm">']
    if demo:
        parts.append('<div class="cm-note"><b>Sample data.</b> This is a made-up storm used to preview the layout, not a real forecast.</div>')
    if error:
        parts.append('<div class="cm-note"><b>The forecast could not be loaded at the last update.</b> '
                     'Check the <a href="https://www.weather.gov/buf/">National Weather Service Buffalo</a> forecast directly.</div>')

    # Active alerts, one line per distinct alert
    seen = set()
    for point_alerts in alerts.values():
        for alert in point_alerts:
            key = alert.get("id") or alert.get("headline")
            if key in seen or alert.get("event") not in ALERT_LEVELS:
                continue
            seen.add(key)
            parts.append(f'<div class="cm-alert"><b>{esc(alert["event"])}</b>{esc(alert.get("headline") or "")}</div>')

    rated = [r for r in rows if any(c["level"] is not None for c in r["cells"])]
    if rated:
        nxt = rated[0]
        level = max(c["level"] for c in nxt["cells"] if c["level"] is not None)
        worst = [c for c in nxt["cells"] if c["level"] == level]
        if level == 0:
            why = "No winter hazards expected on any corridor."
        else:
            reasons = "; ".join(worst[0]["reasons"])
            why = f'<b>{esc(", ".join(c["corridor"] for c in worst))}:</b> {esc(reasons)}.'
        parts.append(
            f'<div class="cm-hero" style="--c:{COLORS[level]}">'
            f'<span class="cm-icon" aria-hidden="true">{ICONS[level]}</span>'
            f'<div><div class="cm-when">Next commute · {nxt["day"].strftime("%A")} {nxt["label"].lower()}, {hours_text(nxt)}</div>'
            f'<div class="cm-big">{LEVELS[level]}</div></div>'
            f'<div class="cm-why">{why}</div></div>'
        )

    # Outlook table: rows are commute windows, columns are corridors
    head = "".join(f"<th>{esc(n)}<small>{esc(note)}</small></th>" for n, note, _ in CORRIDORS)
    parts.append(f'<div class="cm-scroll"><table data-quarto-disable-processing="true"><thead><tr><th></th>{head}</tr></thead><tbody>')
    last_day = None
    for row in rows:
        if row["day"] != last_day:
            last_day = row["day"]
            parts.append(f'<tr class="cm-day"><td colspan="{len(CORRIDORS) + 1}">{row["day"].strftime("%A, %B %-d")}</td></tr>')
        tds = ""
        for cell in row["cells"]:
            detail = ", ".join(cell["reasons"][:3])
            if cell["level"] is not None and cell["metrics"]["snow"] is None:
                detail = (detail + "; " if detail else "") + "snow amount not yet forecast"
            tds += (f'<td class="cm-cell" title="{esc(tooltip(cell["metrics"]))}">{chip(cell["level"])}'
                    f'<small>{esc(detail)}</small></td>')
        parts.append(f'<tr><td class="cm-win">{row["label"]}<small>{hours_text(row)}</small></td>{tds}</tr>')
    parts.append("</tbody></table></div>")

    legend = "".join(
        f'<span><span class="cm-icon" style="--c:{COLORS[i]}" aria-hidden="true">{ICONS[i]}</span>{LEVELS[i]}</span>'
        for i in range(4)
    )
    parts.append(f'<div class="cm-legend">{legend}</div>')
    parts.append(
        f'<div class="cm-foot">Updated {now.strftime("%A, %B %-d at %-I:%M %p")} Eastern. '
        'Hover over a rating for the numbers behind it. Source: National Weather Service. '
        'This is a personal project, not an official forecast. For travel bans and road closures, check Erie County and the City of Buffalo.</div>'
    )
    parts += ["</div>", "```", ""]
    return "\n".join(parts)


def render_method():
    """The thresholds table, built from the settings above so the page never drifts from the code."""
    return "\n".join([
        "```{=html}", STYLE, '<div class="cm">',
        '<p>Each commute window takes the rating of its worst single factor. '
        f'Snow combined with gusts of {BLOWING_SNOW_GUST} mph or more is bumped up one level for blowing snow.</p>'
        '<div class="cm-rules cm-scroll"><table data-quarto-disable-processing="true"><thead><tr><th>Factor</th>'
        + "".join(f"<th>{LEVELS[l]}</th>" for l in (1, 2, 3))
        + f"</tr></thead><tbody>{threshold_rows()}</tbody></table></div>",
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

    def conditions(point, local):
        idx = workdays.index(local.date()) if local.date() in workdays else -1
        h = local.hour
        snow, ice, gust, feels = 0.0, 0.0, 14.0, 27.0
        if idx == 0:
            gust, feels = 42.0, 3.0
            if h < 13:
                snow = {"Orchard Park": 1.6, "Lancaster": 0.8, "Downtown Buffalo": 0.25}.get(point, 0.0)
            elif h < 20:
                snow = {"Orchard Park": 0.4, "Lancaster": 0.15}.get(point, 0.0)
        elif idx == 1:
            gust, feels = 24.0, (-6.0 if h < 11 else 9.0)
        elif idx == 2:
            if 4 <= h < 10 and point in ("Amherst", "Downtown Buffalo"):
                ice = 0.012
            feels = 30.0
        return snow, ice, gust, feels

    start = now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    start -= timedelta(hours=start.hour % 6)
    grids = {}
    for point in POINTS:
        layers = {k: [] for k in ("snow", "ice", "gust", "feels")}
        for block in range(0, 24 * 8, 6):
            block_start = start + timedelta(hours=block)
            snow_total = ice_total = 0.0
            for h in range(6):
                t = block_start + timedelta(hours=h)
                snow, ice, gust, feels = conditions(point, t.astimezone(TZ))
                snow_total += snow
                ice_total += ice
                layers["gust"].append({"validTime": f"{t.isoformat()}/PT1H", "value": gust / 0.621371})
                layers["feels"].append({"validTime": f"{t.isoformat()}/PT1H", "value": (feels - 32) * 5 / 9})
            if block < 132:  # like the real feed, snow and ice totals stop short of the full forecast
                layers["snow"].append({"validTime": f"{block_start.isoformat()}/PT6H", "value": snow_total * 25.4})
                layers["ice"].append({"validTime": f"{block_start.isoformat()}/PT6H", "value": ice_total * 25.4})
        grids[point] = {
            "snowfallAmount": {"uom": "wmoUnit:mm", "values": layers["snow"]},
            "iceAccumulation": {"uom": "wmoUnit:mm", "values": layers["ice"]},
            "windGust": {"uom": "wmoUnit:km_h-1", "values": layers["gust"]},
            "apparentTemperature": {"uom": "wmoUnit:degC", "values": layers["feels"]},
        }
    alerts = {p: [] for p in POINTS}
    if workdays:
        d = workdays[0]
        alerts["Orchard Park"].append({
            "id": "demo-1", "event": "Lake Effect Snow Warning",
            "headline": "Sample alert: Lake Effect Snow Warning for southern Erie County until 7 p.m.",
            "onset": datetime(d.year, d.month, d.day, 1, tzinfo=TZ).isoformat(),
            "ends": datetime(d.year, d.month, d.day, 19, tzinfo=TZ).isoformat(),
        })
    return grids, alerts


def main():
    demo = "--demo" in sys.argv
    now = datetime.now(TZ)
    try:
        grids, alerts = demo_data(now) if demo else fetch_live()
        rows = assess(build_hourly(grids), alerts, now)
        page = render(rows, alerts, now, demo=demo)
    except Exception as err:  # noqa: BLE001  (never break the site build over a bad fetch)
        print(f"WARNING: {err}", file=sys.stderr)
        page = render([], {}, now, error=str(err))
    OUT.write_text(page, encoding="utf-8")
    OUT_METHOD.write_text(render_method(), encoding="utf-8")
    print(f"Wrote {OUT.name} and {OUT_METHOD.name}")


if __name__ == "__main__":
    main()
