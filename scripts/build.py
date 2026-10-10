"""
build.py — Maintain a rolling 24h store of NEA wind data and render an
interactive SVG chart (same pan/zoom engine as the AQ dashboard).

Each run:
  1. loads data/wind.json (the persistent store, committed to the repo)
  2. drops readings older than 24h
  3. works out which time slots in the last 24h are missing and downloads
     only those (so the first run backfills 24h, later runs fill the gap)
  4. saves data/wind.json and writes docs/index.html (data embedded inline)

Environment variables:
  NEA_API_KEY   optional — doubles rate limit from 6 → 12 calls/10 s
  STEP_MINUTES  sampling interval in minutes (default 5)
"""

import os, time, json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────

API_SPEED = "https://api-open.data.gov.sg/v2/real-time/api/wind-speed"
API_DIR   = "https://api-open.data.gov.sg/v2/real-time/api/wind-direction"

API_KEY      = os.environ.get("NEA_API_KEY", "")
STEP_MINUTES = int(os.environ.get("STEP_MINUTES", "5"))
WINDOW       = timedelta(hours=24)
# Slots newer than this may simply not be published yet, so an empty answer
# is not recorded (it will be retried next run). Older empty slots are
# stored as empty so a permanent API outage isn't re-fetched forever.
SETTLE       = timedelta(minutes=30)

SGT = timezone(timedelta(hours=8))
TS_FMT = "%Y-%m-%dT%H:%M"

CALLS_PER_WINDOW = 12 if API_KEY else 6
WINDOW_SECS      = 11
MAX_RETRIES      = 6
BACKOFF_BASE     = 2

DATA_FILE = Path("data/wind.json")
DOCS = Path("docs")

# Speed conversion to km/h, keyed by the unit the API reports.
SPEED_UNIT_TO_KMH = {
    "knot": 1.852, "knots": 1.852, "kt": 1.852, "kts": 1.852,
    "m/s": 3.6, "mps": 3.6, "meter per second": 3.6, "meters per second": 3.6,
    "metre per second": 3.6, "metres per second": 3.6,
    "km/h": 1.0, "kmh": 1.0, "kph": 1.0,
    "kilometer per hour": 1.0, "kilometre per hour": 1.0,
}
DEFAULT_UNIT = "knots"  # NEA's confirmed unit as of 2026-09-12


# ── Fetching ──────────────────────────────────────────────────────────────────

def headers():
    h = {"Accept": "application/json"}
    if API_KEY:
        h["x-api-key"] = API_KEY
    return h


def fetch(endpoint, dt):
    params = {"date": dt.strftime("%Y-%m-%dT%H:%M:%S")}
    for attempt in range(MAX_RETRIES):
        try:
            r = requests.get(endpoint, params=params, headers=headers(), timeout=15)
            if r.status_code == 429:
                wait = BACKOFF_BASE ** (attempt + 1)
                print(f"  [429] backing off {wait}s …", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            wait = BACKOFF_BASE ** attempt
            print(f"  [err] {dt:%H:%M} attempt {attempt+1}: {e}, retry in {wait}s")
            time.sleep(wait)
    return None


def extract(payload):
    """Return ({stationId: value}, {stationId: {name, lat, lon}}, unit)."""
    readings, meta, unit = {}, {}, None
    if not payload:
        return readings, meta, unit
    try:
        data = payload.get("data", {})
        unit = data.get("readingUnit")
        for s in data.get("stations", []):
            sid = s.get("id") or s.get("stationId")
            if sid:
                loc = s.get("location", s.get("labelLocation", {}))
                meta[sid] = {
                    "name": s.get("name", sid),
                    "lat":  float(loc.get("latitude", 0)),
                    "lon":  float(loc.get("longitude", 0)),
                }
        for block in data.get("readings", []):
            for item in block.get("data", []):
                sid = item.get("stationId") or item.get("station_id")
                val = item.get("value")
                if sid and val is not None:
                    readings[sid] = float(val)
    except (KeyError, TypeError, ValueError):
        pass
    return readings, meta, unit


def speed_factor(unit):
    key = (unit or "").strip().lower()
    if key in SPEED_UNIT_TO_KMH:
        return SPEED_UNIT_TO_KMH[key]
    if unit:
        print(f"  ⚠ unrecognised speed unit '{unit}', assuming {DEFAULT_UNIT}")
    return SPEED_UNIT_TO_KMH[DEFAULT_UNIT]


# ── Store ─────────────────────────────────────────────────────────────────────

def load_store():
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text())
        except json.JSONDecodeError:
            print("  ⚠ data/wind.json is corrupt, starting fresh")
    return {"stations": {}, "readings": {}}


def save_store(store):
    DATA_FILE.parent.mkdir(exist_ok=True)
    DATA_FILE.write_text(json.dumps(store, separators=(",", ":"), sort_keys=True))


def expected_slots(now):
    """All slot times in the last 24h, aligned to STEP_MINUTES, oldest first."""
    step = timedelta(minutes=STEP_MINUTES)
    end = now.replace(second=0, microsecond=0)
    end -= timedelta(minutes=end.minute % STEP_MINUTES)
    start = end - WINDOW
    slots, t = [], start
    while t <= end:
        slots.append(t)
        t += step
    return slots


def update(store, now):
    slots = expected_slots(now)
    keep = {t.strftime(TS_FMT) for t in slots}
    store["readings"] = {k: v for k, v in store["readings"].items() if k >= min(keep)}

    missing = [t for t in slots if t.strftime(TS_FMT) not in store["readings"]]
    print(f"  Window: {slots[0]:{TS_FMT}} → {slots[-1]:{TS_FMT}} SGT "
          f"({len(slots)} slots, {len(missing)} missing)")
    if not missing:
        return

    print(f"  Fetching {len(missing)} slots × 2 endpoints "
          f"({CALLS_PER_WINDOW} calls/10s)\n")
    batch_size = max(1, CALLS_PER_WINDOW // 2)
    unit = store.get("speed_unit")

    for b in range(0, len(missing), batch_size):
        for t in missing[b:b + batch_size]:
            sp, sp_meta, sp_unit = extract(fetch(API_SPEED, t))
            dr, dr_meta, _       = extract(fetch(API_DIR, t))
            for sid, info in {**sp_meta, **dr_meta}.items():
                store["stations"].setdefault(sid, info)
            if sp_unit:
                unit = sp_unit
            factor = speed_factor(sp_unit or unit)

            key = t.strftime(TS_FMT)
            if sp or dr:
                store["readings"][key] = {
                    "s": {k: round(v * factor, 1) for k, v in sp.items()},
                    "d": {k: round(v) % 360 for k, v in dr.items()},
                }
            elif now - t > SETTLE:
                store["readings"][key] = {"s": {}, "d": {}}
            print(f"  {key}  speed:{len(sp):3d}  dir:{len(dr):3d}", flush=True)
        if b + batch_size < len(missing):
            time.sleep(WINDOW_SECS)

    store["speed_unit"] = unit
    store["speed_display_unit"] = "km/h"


# ── HTML ──────────────────────────────────────────────────────────────────────

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Singapore Wind – Past 24 Hours</title>
<style>
  :root {
    color-scheme: light;
    --bg: #f9f9f7; --surface: #fcfcfb; --ink: #0b0b0b; --muted: #52514e;
    --faint: #898781; --grid: #e1e0d9; --border: rgba(11,11,11,0.10);
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --bg: #0d0d0d; --surface: #1a1a19; --ink: #ffffff; --muted: #c3c2b7;
      --faint: #898781; --grid: #2c2c2a; --border: rgba(255,255,255,0.10);
    }
  }
  * { box-sizing: border-box; }
  body { margin: 0; background: var(--bg); color: var(--ink);
         font-family: system-ui, -apple-system, "Segoe UI", sans-serif; }
  main { max-width: 900px; margin: 0 auto; padding: 20px 16px 40px; }
  header { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
  h1 { font-size: 1.4rem; margin: 0 0 4px; }
  .subtitle { color: var(--muted); font-size: 0.9rem; margin: 0 0 12px; }
  button { font: inherit; font-size: 0.8rem; padding: 6px 10px; border-radius: 6px;
           border: 1px solid var(--border); background: var(--surface); color: var(--ink); cursor: pointer; }
  .toolbar { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; margin-bottom: 8px;
             font-size: 0.8rem; color: var(--muted); }
  .toolbar .spacer { flex: 1; }
  .chart-card { background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
                padding: 10px 12px 4px; }
  .panel-title { font-size: 0.85rem; color: var(--muted); text-align: center; margin: 2px 0 4px; }
  svg.chart { width: 100%; height: auto; display: block; touch-action: pan-y; cursor: grab;
              user-select: none; -webkit-user-select: none; }
  svg.chart.dragging { cursor: grabbing; }
  .axis-label { font-size: 10px; fill: var(--faint); }
  .tooltip { position: fixed; pointer-events: none; background: var(--ink); color: var(--bg);
             font-size: 0.78rem; padding: 6px 9px; border-radius: 6px; line-height: 1.5;
             z-index: 20; white-space: nowrap; }
  .tooltip .row { display: flex; gap: 10px; justify-content: space-between; }
  .tooltip .row .k { opacity: 0.85; }
  .tooltip .row .v { font-weight: 600; font-variant-numeric: tabular-nums; }
  .tooltip .sep { border-top: 1px solid currentColor; opacity: 0.3; margin: 3px 0; }
  .panel { margin-top: 14px; border: 1px solid var(--border); border-radius: 10px; background: var(--surface); }
  summary { padding: 10px 12px; cursor: pointer; font-weight: 600; }
  .bar { padding: 0 12px 8px; display: flex; gap: 8px; flex-wrap: wrap; }
  .group { padding: 0 12px 10px; }
  .ghead { display: flex; align-items: center; gap: 8px; margin: 6px 0; }
  .ghead h3 { margin: 0; font-size: 13px; flex: 1; }
  .ghead button { padding: 3px 9px; font-size: 12px; }
  .chips { display: grid; gap: 6px; grid-template-columns: repeat(auto-fill, minmax(210px, 1fr)); }
  .chip { display: flex; align-items: center; gap: 8px; padding: 8px 10px; min-height: 38px;
          text-align: left; border-radius: 20px; font-size: 13px; }
  .chip i { width: 12px; height: 12px; border-radius: 50%; flex: none; }
  .chip.avg { font-weight: 600; border-width: 2px; }
  .chip.off { opacity: .45; }
  .chip.off i { background: transparent !important; border: 2px solid var(--faint); }
  .hint { margin-top: 12px; color: var(--muted); font-size: 0.78rem; line-height: 1.5; }
  .empty-state { color: var(--muted); font-size: 0.9rem; padding: 24px 0; text-align: center; }
  @media (max-width: 640px) {
    main { padding: 12px 8px 30px; }
    h1 { font-size: 1.2rem; }
    .chips { grid-template-columns: 1fr 1fr; }
    .chip { font-size: 12px; padding: 6px 8px; border-radius: 10px; }
    .group { padding: 0 8px 8px; }
  }
</style>
</head>
<body>
<main>
  <header>
    <div>
      <h1>Singapore wind &ndash; past 24 hours</h1>
      <p class="subtitle" id="subtitle"></p>
    </div>
  </header>

  <div class="toolbar">
    <button id="zout" type="button" aria-label="Zoom out">&minus;</button>
    <button id="zin" type="button" aria-label="Zoom in">+</button>
    <button data-hours="3" type="button">3h</button>
    <button data-hours="6" type="button">6h</button>
    <button data-hours="12" type="button">12h</button>
    <button data-hours="24" type="button">24h</button>
    <span class="spacer"></span>
    <button id="reset" type="button">Reset view</button>
  </div>

  <div class="chart-card" id="card">
    <div class="panel-title">Wind speed (km/h) &mdash; thick lines are each region's vector-mean wind</div>
    <svg class="chart" id="chart-speed"></svg>
    <div class="panel-title">Wind direction (&deg;, the bearing the wind blows from)</div>
    <svg class="chart" id="chart-dir"></svg>
  </div>

  <details class="panel" id="stationPanel" open>
    <summary>Stations</summary>
    <div class="bar">
      <button id="all" type="button">Show all</button>
      <button id="none" type="button">Hide all</button>
    </div>
    <div id="chips"></div>
  </details>

  <div class="hint" id="hint"></div>
</main>

<div class="tooltip" id="tooltip" hidden></div>

<script>
(function () {
  "use strict";

  var STORE = __DATA__;
  var COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
  var REGION_COLORS = { North: "#d62728", West: "#1f4e9c", Central: "#8e24aa", East: "#e08a00", South: "#0b8f6a" };
  var MARGIN = { top: 10, right: 12, bottom: 6, left: 44 };
  var BOTTOM_AXIS_H = 28;
  var H_SPEED = 250, H_DIR = 230;
  var MIN_SPAN_MS = 20 * 60 * 1000;
  var TAP_MAX_MOVE_PX = 8;
  var MIN_PX_PER_TICK = 62;
  var STEP_CANDIDATES_MS = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440].map(function (m) { return m * 60000; });
  var DAY_MS = 86400000;

  // Readings are stored as SGT wall-clock strings; parse them as UTC and
  // format with timeZone "UTC" so the wall-clock time is shown unchanged.
  function toMs(s) { return Date.parse(s + ":00Z"); }
  function fmt(ms, opts) {
    return new Intl.DateTimeFormat("en-GB", Object.assign({ timeZone: "UTC" }, opts)).format(new Date(ms));
  }
  var fmtHM = function (ms) { return fmt(ms, { hour: "2-digit", minute: "2-digit", hour12: false }); };
  var fmtDay = function (ms) { return fmt(ms, { day: "numeric", month: "short" }); };
  var compass = function (d) { return COMPASS[Math.round(d / 22.5) % 16]; };

  var els = {};
  var state = { width: 860, viewDomain: null, fullDomain: null, panels: [], pinned: false };

  var times = Object.keys(STORE.readings).sort();
  var tms = times.map(toMs);
  var rank = function (id) { return STORE.regions.indexOf(STORE.stations[id].region); };
  var ids = Object.keys(STORE.stations).sort(function (a, b) {
    return rank(a) - rank(b) || STORE.stations[a].name.localeCompare(STORE.stations[b].name);
  });

  function readings(t, key, id) { var r = (STORE.readings[t] || {})[key]; return r && r[id] != null ? r[id] : null; }

  var stations = ids.map(function (id, i) {
    return {
      id: id, name: STORE.stations[id].name, region: STORE.stations[id].region,
      color: "hsl(" + ((i * 137.5) % 360) + " 65% 48%)", on: true, isAvg: false,
      sp: times.map(function (t) { return readings(t, "s", id); }),
      dr: times.map(function (t) { return readings(t, "d", id); })
    };
  });

  // Vector-mean wind per region and time step: average the u/v components of
  // every station reporting both speed and direction, then convert back.
  var activeRegions = STORE.regions.filter(function (r) {
    return ids.some(function (id) { return STORE.stations[id].region === r; });
  });
  var averages = activeRegions.map(function (region) {
    var members = ids.filter(function (id) { return STORE.stations[id].region === region; });
    var s = { name: region + " average", region: region, color: REGION_COLORS[region], on: true, isAvg: true,
              sp: [], dr: [], cnt: [] };
    times.forEach(function (t) {
      var u = 0, v = 0, n = 0;
      members.forEach(function (id) {
        var sp = readings(t, "s", id), d = readings(t, "d", id);
        if (sp == null || d == null) return;
        var rad = d * Math.PI / 180;
        u += -sp * Math.sin(rad); v += -sp * Math.cos(rad); n++;
      });
      if (!n) { s.sp.push(null); s.dr.push(null); s.cnt.push(0); return; }
      u /= n; v /= n;
      s.sp.push(Math.round(Math.hypot(u, v) * 10) / 10);
      s.dr.push(Math.round((Math.atan2(-u, -v) * 180 / Math.PI + 360) % 360) % 360);
      s.cnt.push(n);
    });
    return s;
  });
  var allSeries = stations.concat(averages);

  document.addEventListener("DOMContentLoaded", init);

  function init() {
    els.subtitle = document.getElementById("subtitle");
    els.tooltip = document.getElementById("tooltip");
    var have = times.filter(function (t) { return Object.keys(STORE.readings[t].s || {}).length; }).length;
    els.subtitle.textContent = ids.length + " stations · " + have + "/" + times.length + " slots · " +
      (times.length ? times[0].replace("T", " ") + " → " + times[times.length - 1].replace("T", " ") + " SGT" : "no data yet");

    if (!times.length) {
      var p = document.createElement("p");
      p.className = "empty-state"; p.textContent = "No wind data yet.";
      document.getElementById("card").replaceWith(p);
      return;
    }

    state.fullDomain = [tms[0], tms[tms.length - 1]];
    state.viewDomain = state.fullDomain.slice();

    var touch = matchMedia("(pointer: coarse)").matches;
    document.getElementById("hint").innerHTML = touch
      ? "Swipe sideways on a chart to pan, vertically to scroll the page; use the &minus;/+ and 3h/6h/12h/24h buttons to zoom. Tap a point for readings. Speed in km/h; direction is the bearing the wind blows <em>from</em>."
      : "Drag to pan, scroll or pinch to zoom, click to pin the readings, hover for exact values. Thick lines and dots are the vector-mean wind of each region. Speed in km/h; direction is the bearing the wind blows <em>from</em>.";

    document.getElementById("zin").onclick = function () { zoomBy(0.5); };
    document.getElementById("zout").onclick = function () { zoomBy(2); };
    document.getElementById("reset").onclick = resetView;
    document.querySelectorAll(".toolbar [data-hours]").forEach(function (b) {
      b.onclick = function () {
        var h = +b.dataset.hours, hi = state.fullDomain[1];
        state.viewDomain = clampViewDomain([hi - h * 3600000, hi]);
        dismissPinned(); renderAll();
      };
    });
    window.addEventListener("resize", debounce(function () { measureWidth(); renderAll(); }, 150));
    document.addEventListener("pointerdown", function (evt) {
      if (!state.pinned) return;
      if (evt.target.closest && evt.target.closest(".chart-card")) return;
      dismissPinned();
    });

    buildChips();
    if (innerWidth <= 640) document.getElementById("stationPanel").open = false;
    measureWidth();
    buildPanel("chart-speed", false);
    buildPanel("chart-dir", true);
    renderAll();
  }

  function debounce(fn, ms) {
    var t;
    return function () { clearTimeout(t); t = setTimeout(fn, ms); };
  }
  function measureWidth() { state.width = Math.max(280, document.getElementById("card").clientWidth - 24); }
  function dismissPinned() {
    state.pinned = false; els.tooltip.hidden = true;
    state.panels.forEach(function (p) { if (p.crosshair) p.crosshair.setAttribute("visibility", "hidden"); });
  }
  function resetView() { state.viewDomain = state.fullDomain.slice(); dismissPinned(); renderAll(); }

  function niceTicks(min, max, count) {
    if (min === max) { min -= 1; max += 1; }
    var step0 = (max - min) / count;
    var mag = Math.pow(10, Math.floor(Math.log10(step0)));
    var residual = step0 / mag;
    var step = residual > 5 ? 10 * mag : residual > 2 ? 5 * mag : residual > 1 ? 2 * mag : mag;
    var ticks = [];
    for (var v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + 1e-9; v += step) ticks.push(Math.round(v * 1000) / 1000);
    return ticks;
  }

  function timeTicks(domain, innerW) {
    var span = domain[1] - domain[0];
    var maxTicks = Math.max(2, Math.floor(innerW / MIN_PX_PER_TICK));
    var step = STEP_CANDIDATES_MS[STEP_CANDIDATES_MS.length - 1];
    for (var i = 0; i < STEP_CANDIDATES_MS.length; i++) {
      if (span / STEP_CANDIDATES_MS[i] <= maxTicks) { step = STEP_CANDIDATES_MS[i]; break; }
    }
    var ticks = [];
    for (var t = Math.ceil(domain[0] / step) * step; t <= domain[1]; t += step) ticks.push(t);
    return ticks;
  }

  function scaleLinear(domain, range) {
    var span = domain[1] - domain[0] || 1;
    return function (v) { return range[0] + ((v - domain[0]) / span) * (range[1] - range[0]); };
  }
  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) el.setAttribute(k, attrs[k]);
    return el;
  }

  function linePath(vals, x, y) {
    var d = "", pen = false;
    for (var i = 0; i < vals.length; i++) {
      if (vals[i] == null) { pen = false; continue; }
      d += (pen ? "L" : "M") + x(tms[i]).toFixed(1) + "," + y(vals[i]).toFixed(1) + " ";
      pen = true;
    }
    return d;
  }
  // Direction points are drawn as zero-length round-capped segments: one
  // <path> per series instead of hundreds of <circle> nodes.
  function dotPath(vals, x, y, lo, hi) {
    var d = "";
    for (var i = lo; i <= hi; i++) {
      if (vals[i] == null) continue;
      d += "M" + x(tms[i]).toFixed(1) + "," + y(vals[i]).toFixed(1) + "h0";
    }
    return d;
  }

  function visibleRange() {
    var lo = 0, hi = tms.length - 1;
    while (lo < tms.length && tms[lo] < state.viewDomain[0]) lo++;
    while (hi >= 0 && tms[hi] > state.viewDomain[1]) hi--;
    return [Math.max(0, lo - 1), Math.min(tms.length - 1, hi + 1)];
  }

  function speedYDomain() {
    var r = visibleRange(), max = 0;
    allSeries.forEach(function (s) {
      if (!s.on) return;
      for (var i = r[0]; i <= r[1]; i++) if (s.sp[i] != null && s.sp[i] > max) max = s.sp[i];
    });
    var ticks = niceTicks(0, Math.max(max * 1.05, 5), 4);
    return [0, ticks[ticks.length - 1]];
  }

  function panelInnerH(isDir) { return (isDir ? H_DIR : H_SPEED) - MARGIN.top - MARGIN.bottom - (isDir ? BOTTOM_AXIS_H : 0); }

  function buildPanel(id, isDir) {
    var svg = document.getElementById(id);
    var g = svgEl("g");
    var clipRect = svgEl("rect");
    var clipPath = svgEl("clipPath", { id: id + "-clip" });
    var defs = svgEl("defs");
    clipPath.appendChild(clipRect); defs.appendChild(clipPath);
    svg.appendChild(g); svg.appendChild(defs);
    var gridGroup = svgEl("g");
    var plot = svgEl("g", { "clip-path": "url(#" + id + "-clip)" });
    var chromeGroup = svgEl("g");
    g.appendChild(gridGroup); g.appendChild(plot); g.appendChild(chromeGroup);
    var panel = { svg: svg, g: g, gridGroup: gridGroup, plot: plot, chromeGroup: chromeGroup, clipRect: clipRect, isDir: isDir };
    state.panels.push(panel);
    attachInteraction(panel);
  }

  function renderAll() { state.panels.forEach(renderPanel); }

  function renderPanel(panel) {
    var W = state.width, H = panel.isDir ? H_DIR : H_SPEED;
    var innerW = W - MARGIN.left - MARGIN.right, innerH = panelInnerH(panel.isDir);
    panel.svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    panel.svg.setAttribute("width", W);
    panel.svg.setAttribute("height", H);
    panel.g.setAttribute("transform", "translate(" + MARGIN.left + "," + MARGIN.top + ")");
    panel.clipRect.setAttribute("x", -2); panel.clipRect.setAttribute("y", -4);
    panel.clipRect.setAttribute("width", innerW + 4); panel.clipRect.setAttribute("height", innerH + 8);
    panel.gridGroup.innerHTML = ""; panel.plot.innerHTML = ""; panel.chromeGroup.innerHTML = "";

    var x = scaleLinear(state.viewDomain, [0, innerW]);
    var yDomain = panel.isDir ? [0, 360] : speedYDomain();
    var y = scaleLinear(yDomain, [innerH, 0]);
    panel.x = x; panel.y = y; panel.innerW = innerW; panel.innerH = innerH;

    var yTicks = panel.isDir ? [0, 90, 180, 270, 360] : niceTicks(yDomain[0], yDomain[1], 4);
    var dirLabels = { 0: "N", 90: "E", 180: "S", 270: "W", 360: "N" };
    yTicks.forEach(function (t) {
      panel.gridGroup.appendChild(svgEl("line", { x1: 0, x2: innerW, y1: y(t), y2: y(t), stroke: "var(--grid)", "stroke-width": 1 }));
      var lbl = svgEl("text", { class: "axis-label", x: -6, y: y(t) + 3, "text-anchor": "end" });
      lbl.textContent = panel.isDir ? dirLabels[t] + " " + t + "°" : Math.round(t);
      panel.chromeGroup.appendChild(lbl);
    });

    timeTicks(state.viewDomain, innerW).forEach(function (t) {
      var xp = x(t);
      panel.gridGroup.appendChild(svgEl("line", { x1: xp, x2: xp, y1: 0, y2: innerH, stroke: "var(--grid)", "stroke-width": 1 }));
      if (panel.isDir) {
        panel.chromeGroup.appendChild(svgEl("line", { x1: xp, x2: xp, y1: innerH, y2: innerH + 4, stroke: "var(--faint)", "stroke-width": 1 }));
        var midnight = fmtHM(t) === "00:00";
        var lbl = svgEl("text", { class: "axis-label", x: xp, y: innerH + 15, "text-anchor": "middle" });
        lbl.textContent = fmtHM(t);
        panel.chromeGroup.appendChild(lbl);
        if (midnight) {
          var d = svgEl("text", { class: "axis-label", x: xp, y: innerH + 26, "text-anchor": "middle" });
          d.textContent = fmtDay(t);
          panel.chromeGroup.appendChild(d);
        }
      }
    });

    var r = visibleRange();
    // Stations first, region averages on top.
    allSeries.forEach(function (s) {
      if (!s.on) return;
      if (panel.isDir) {
        panel.plot.appendChild(svgEl("path", {
          d: dotPath(s.dr, x, y, r[0], r[1]), fill: "none", stroke: s.color,
          "stroke-width": s.isAvg ? 7 : 4, "stroke-linecap": "round", opacity: s.isAvg ? 1 : 0.8
        }));
      } else {
        panel.plot.appendChild(svgEl("path", {
          d: linePath(s.sp, x, y), fill: "none", stroke: s.color,
          "stroke-width": s.isAvg ? 3.5 : 1.4, "stroke-linejoin": "round", "stroke-linecap": "round",
          opacity: s.isAvg ? 1 : 0.85
        }));
      }
    });

    var crosshair = svgEl("line", { x1: -10, x2: -10, y1: 0, y2: innerH, stroke: "var(--faint)", "stroke-width": 1, visibility: "hidden" });
    panel.chromeGroup.appendChild(crosshair);
    panel.crosshair = crosshair;
  }

  function clampViewDomain(domain) {
    var full = state.fullDomain;
    var span = Math.max(Math.min(domain[1] - domain[0], full[1] - full[0]), MIN_SPAN_MS);
    var lo = domain[0], hi = lo + span;
    if (lo < full[0]) { lo = full[0]; hi = lo + span; }
    if (hi > full[1]) { hi = full[1]; lo = hi - span; }
    return [lo, hi];
  }
  function localXFromClientX(svg, clientX) {
    var rect = svg.getBoundingClientRect();
    return (clientX - rect.left) * (state.width / rect.width) - MARGIN.left;
  }
  function localYFromClientY(svg, clientY) {
    var rect = svg.getBoundingClientRect();
    return (clientY - rect.top) * (state.width / rect.width) - MARGIN.top;
  }
  function zoomAtFrac(frac, factor) {
    frac = Math.max(0, Math.min(1, frac));
    var d = state.viewDomain, cursorT = d[0] + frac * (d[1] - d[0]), span = (d[1] - d[0]) * factor;
    state.viewDomain = clampViewDomain([cursorT - frac * span, cursorT - frac * span + span]);
    renderAll();
  }
  function zoomBy(f) { zoomAtFrac(0.5, f); }

  // Single pointer = pan. Two pointers = pinch-zoom around their midpoint.
  function attachInteraction(panel) {
    var svg = panel.svg, pointers = {}, dragState = null, pinchState = null;
    var activeIds = function () { return Object.keys(pointers); };
    var dist = function (a, b) { return Math.hypot(pointers[a].x - pointers[b].x, pointers[a].y - pointers[b].y); };

    svg.addEventListener("pointerdown", function (evt) {
      evt.preventDefault();
      try { svg.setPointerCapture(evt.pointerId); } catch (e) {}
      pointers[evt.pointerId] = { x: evt.clientX, y: evt.clientY };
      var a = activeIds();
      if (a.length === 2) {
        dragState = null; pinchState = { ids: a, lastDist: dist(a[0], a[1]) };
        svg.classList.remove("dragging"); panel.tapStart = null;
      } else if (a.length === 1) {
        pinchState = null; svg.classList.add("dragging");
        dragState = { startX: evt.clientX, startDomain: state.viewDomain.slice() };
        panel.tapStart = { x: evt.clientX, y: evt.clientY };
      }
    });

    svg.addEventListener("pointermove", function (evt) {
      if (!(evt.pointerId in pointers)) { handleHover(panel, evt); return; }
      pointers[evt.pointerId] = { x: evt.clientX, y: evt.clientY };
      if (panel.tapStart && Math.hypot(evt.clientX - panel.tapStart.x, evt.clientY - panel.tapStart.y) > TAP_MAX_MOVE_PX) panel.tapStart = null;

      if (pinchState) {
        evt.preventDefault();
        var p = pinchState.ids;
        if (!(p[0] in pointers) || !(p[1] in pointers)) return;
        var d = dist(p[0], p[1]);
        if (pinchState.lastDist > 0 && d > 0) {
          var mid = (pointers[p[0]].x + pointers[p[1]].x) / 2;
          zoomAtFrac(localXFromClientX(svg, mid) / panel.innerW, pinchState.lastDist / d);
        }
        pinchState.lastDist = d;
        return;
      }
      if (dragState) {
        evt.preventDefault();
        var scale = state.width / svg.getBoundingClientRect().width;
        var dtMs = ((evt.clientX - dragState.startX) * scale) / (panel.innerW / (dragState.startDomain[1] - dragState.startDomain[0]));
        state.viewDomain = clampViewDomain([dragState.startDomain[0] - dtMs, dragState.startDomain[1] - dtMs]);
        renderAll();
        return;
      }
      handleHover(panel, evt);
    });

    function endPointer(evt) {
      try { svg.releasePointerCapture(evt.pointerId); } catch (e) {}
      delete pointers[evt.pointerId];
      var a = activeIds();
      var wasTap = evt.type === "pointerup" && !pinchState && panel.tapStart && a.length === 0;
      if (pinchState) {
        if (a.length < 2) {
          pinchState = null;
          if (a.length === 1) { dragState = { startX: pointers[a[0]].x, startDomain: state.viewDomain.slice() }; svg.classList.add("dragging"); }
          else svg.classList.remove("dragging");
        }
      } else if (dragState && a.length === 0) { dragState = null; svg.classList.remove("dragging"); }
      if (wasTap) { panel.tapStart = null; state.pinned = true; showReadings(panel, evt); }
    }
    svg.addEventListener("pointerup", endPointer);
    svg.addEventListener("pointercancel", endPointer);
    svg.addEventListener("pointerleave", function () {
      if (state.pinned) return;
      state.panels.forEach(function (p) { if (p.crosshair) p.crosshair.setAttribute("visibility", "hidden"); });
      els.tooltip.hidden = true;
    });
    svg.addEventListener("wheel", function (evt) {
      evt.preventDefault();
      zoomAtFrac(localXFromClientX(svg, evt.clientX) / panel.innerW, evt.deltaY > 0 ? 1.15 : 1 / 1.15);
    }, { passive: false });
  }

  function handleHover(panel, evt) { if (!state.pinned) showReadings(panel, evt); }

  function showReadings(panel, evt) {
    var lx = Math.max(0, Math.min(panel.innerW, localXFromClientX(panel.svg, evt.clientX)));
    var d = state.viewDomain, tMs = d[0] + (lx / panel.innerW) * (d[1] - d[0]);
    var idx = 0, best = Infinity;
    for (var i = 0; i < tms.length; i++) { var diff = Math.abs(tms[i] - tMs); if (diff < best) { best = diff; idx = i; } }

    state.panels.forEach(function (p) {
      if (!p.crosshair) return;
      var cx = p.x(tms[idx]);
      p.crosshair.setAttribute("x1", cx); p.crosshair.setAttribute("x2", cx);
      p.crosshair.setAttribute("visibility", "visible");
    });

    var tip = els.tooltip;
    tip.innerHTML = "";
    function row(label, color, value, bold) {
      var r = document.createElement("div"); r.className = "row";
      var k = document.createElement("span"); k.className = "k"; k.textContent = label;
      if (color) k.style.color = color;
      var v = document.createElement("span"); v.className = "v"; v.textContent = value;
      r.appendChild(k); r.appendChild(v); tip.appendChild(r);
    }
    row("Time", null, fmtDay(tms[idx]) + " " + fmtHM(tms[idx]) + " SGT");
    var any = false;
    function reading(s) {
      var sp = s.sp[idx], dr = s.dr[idx];
      return (sp == null ? "–" : sp.toFixed(1) + " km/h") + " · " + (dr == null ? "–" : compass(dr) + " " + dr + "°");
    }
    averages.forEach(function (s) {
      if (!s.on) return;
      any = true; row(s.region + " avg", s.color, reading(s));
    });

    // Many stations would make the tooltip huge: show just the one whose
    // line/dot is closest to the pointer in the hovered panel.
    var ly = localYFromClientY(panel.svg, evt.clientY), near = null, nearD = 24;
    stations.forEach(function (s) {
      var v = panel.isDir ? s.dr[idx] : s.sp[idx];
      if (!s.on || v == null) return;
      var dy = Math.abs(panel.y(v) - ly);
      if (dy < nearD) { nearD = dy; near = s; }
    });
    if (near) {
      if (any) { var sep = document.createElement("div"); sep.className = "sep"; tip.appendChild(sep); }
      any = true; row(near.name + " (" + near.region + ")", near.color, reading(near));
    }
    if (!any) { tip.hidden = true; return; }

    tip.hidden = false;
    var w = tip.offsetWidth || 220, left = evt.clientX + 14;
    if (left + w > window.innerWidth) left = Math.max(4, evt.clientX - w - 14);
    tip.style.left = left + "px";
    tip.style.top = (evt.clientY + 14) + "px";
  }

  function buildChips() {
    var chips = document.getElementById("chips");
    function setAll(v) { allSeries.forEach(function (s) { s.on = v; }); refreshChips(); renderAll(); }
    document.getElementById("all").onclick = function () { setAll(true); };
    document.getElementById("none").onclick = function () { setAll(false); };

    function chip(s) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "chip" + (s.isAvg ? " avg" : "");
      var dot = document.createElement("i"); dot.style.background = s.color;
      var label = document.createElement("span");
      label.textContent = s.isAvg ? s.region : s.name;
      b.appendChild(dot); b.appendChild(label);
      b.onclick = function () { s.on = !s.on; refreshChips(); renderAll(); };
      s.chip = b;
      return b;
    }
    if (averages.length) {
      var ag = document.createElement("div"); ag.className = "group";
      var ahead = document.createElement("div"); ahead.className = "ghead";
      var ah3 = document.createElement("h3"); ah3.textContent = "Region averages (vector mean)";
      function setAvg(v) { averages.forEach(function (s) { s.on = v; }); refreshChips(); renderAll(); }
      var aAll = document.createElement("button"); aAll.type = "button"; aAll.textContent = "All";
      aAll.onclick = function () { setAvg(true); };
      var aNone = document.createElement("button"); aNone.type = "button"; aNone.textContent = "None";
      aNone.onclick = function () { setAvg(false); };
      ahead.appendChild(ah3); ahead.appendChild(aAll); ahead.appendChild(aNone);
      var agrid = document.createElement("div"); agrid.className = "chips";
      averages.forEach(function (s) { agrid.appendChild(chip(s)); });
      ag.appendChild(ahead); ag.appendChild(agrid); chips.appendChild(ag);
    }
    STORE.regions.forEach(function (region) {
      var members = stations.filter(function (s) { return s.region === region; });
      if (!members.length) return;
      var avg = averages.filter(function (s) { return s.region === region; })[0];
      var g = document.createElement("div"); g.className = "group";
      var head = document.createElement("div"); head.className = "ghead";
      var h3 = document.createElement("h3");
      h3.textContent = region + " (" + members.length + ")";
      var only = document.createElement("button"); only.type = "button"; only.textContent = "Only";
      only.onclick = function () {
        allSeries.forEach(function (s) { s.on = false; });
        members.forEach(function (s) { s.on = true; }); avg.on = true;
        refreshChips(); renderAll();
      };
      var tog = document.createElement("button"); tog.type = "button"; tog.textContent = "Toggle";
      tog.onclick = function () {
        var all = members.every(function (s) { return s.on; });
        members.forEach(function (s) { s.on = !all; }); refreshChips(); renderAll();
      };
      head.appendChild(h3); head.appendChild(only); head.appendChild(tog);
      var grid = document.createElement("div"); grid.className = "chips";
      members.forEach(function (s) { grid.appendChild(chip(s)); });
      g.appendChild(head); g.appendChild(grid); chips.appendChild(g);
    });
  }
  function refreshChips() { allSeries.forEach(function (s) { if (s.chip) s.chip.classList.toggle("off", !s.on); }); }
})();
</script>
</body>
</html>
"""


REGIONS = ["North", "West", "Central", "East", "South"]
REGION_OVERRIDES = {
    "S104": "North",
    "S115": "West", "S117": "West", "S44": "West", "S23": "West", "S121": "West", "S50": "West",
    "S111": "Central", "S109": "Central",
    "S43": "East", "S06": "East", "S107": "East", "S106": "East",
    "S116": "South", "S102": "South", "S60": "South", "S108": "South",
}


def region_of(sid, info):
    if sid in REGION_OVERRIDES:
        return REGION_OVERRIDES[sid]
    lat, lon = info.get("lat", 0), info.get("lon", 0)
    if lat > 1.42:  return "North"
    if lon < 103.78: return "West"
    if lon > 103.88: return "East"
    if lat < 1.29:  return "South"
    return "Central"


def render(store):
    DOCS.mkdir(exist_ok=True)
    store = {**store, "stations": {sid: {**info, "region": region_of(sid, info)}
                                   for sid, info in store["stations"].items()},
             "regions": REGIONS}
    payload = json.dumps(store, separators=(",", ":")).replace("</", "<\\/")
    html = HTML.replace("__DATA__", payload)
    (DOCS / "index.html").write_text(html, encoding="utf-8")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    now = datetime.now(SGT).replace(tzinfo=None)
    print(f"\n  SG Wind — now {now:{TS_FMT}} SGT, step {STEP_MINUTES} min, "
          f"API key: {'yes' if API_KEY else 'no'}\n")
    store = load_store()
    update(store, now)
    store["updated"] = now.strftime(TS_FMT)
    save_store(store)
    render(store)
    print(f"\n  ✓ {len(store['readings'])} slots stored, site written to docs/index.html\n")


if __name__ == "__main__":
    main()
