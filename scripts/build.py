"""
build.py — Maintain a rolling 24h store of NEA wind data and render an
interactive chart.

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

HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Singapore Wind – Past 24 Hours</title>
<script>__PLOTLY__</script>
<style>
  :root { --bg:#fff; --fg:#1f2328; --muted:#59636e; --line:#d0d7de; --panel:#f6f8fa; }
  @media (prefers-color-scheme: dark) {
    :root { --bg:#0d1117; --fg:#e6edf3; --muted:#9198a1; --line:#30363d; --panel:#161b22; }
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  header { padding:14px 16px 4px; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:var(--muted); font-size:13px; }
  .zoombar { padding:6px 16px 0; display:flex; gap:6px; }
  .zoombar button { flex:1; padding:8px 0; font-weight:600; }
  .zoombar button.active { background:var(--fg); color:var(--bg); }
  .lockbar { padding:6px 16px 0; }
  .lockbar button { width:100%; }
  .lockbar button.unlocked { background:var(--fg); color:var(--bg); }
  #chart { width:100%; height:max(480px, calc(100vh - 230px)); }
  .panel { margin:0 16px 12px; border:1px solid var(--line); border-radius:8px; background:var(--panel); }
  summary { padding:10px 12px; cursor:pointer; font-weight:600; }
  .bar { padding:0 12px 8px; display:flex; gap:8px; flex-wrap:wrap; }
  button { background:var(--bg); color:var(--fg); border:1px solid var(--line); border-radius:6px;
           padding:8px 12px; cursor:pointer; font:inherit; min-height:36px; }
  .group { padding:0 12px 10px; }
  .ghead { display:flex; align-items:center; gap:8px; margin:6px 0; }
  .ghead h3 { margin:0; font-size:13px; flex:1; }
  .ghead button { min-height:30px; padding:4px 10px; font-size:12px; }
  .chips { padding:0; display:grid; gap:6px; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); }
  .chip { display:flex; align-items:center; gap:8px; padding:8px 10px; min-height:40px; text-align:left;
          border-radius:20px; font-size:13px; }
  .chip i { width:12px; height:12px; border-radius:50%; flex:none; }
  .chip.avg { font-weight:600; border-width:2px; }
  .chip.off { opacity:.45; }
  .chip.off i { background:transparent !important; border:2px solid var(--muted); }
  .hint { padding:0 16px 20px; color:var(--muted); font-size:12px; }
  @media (max-width:640px) {
    header { padding:10px 12px 2px; }
    h1 { font-size:18px; }
    #chart { height:max(420px, 72vh); }
    .panel { margin:0 8px 10px; }
    .zoombar, .lockbar { padding-left:8px; padding-right:8px; }
    .chips { grid-template-columns:1fr 1fr; }
    .group { padding:0 8px 8px; }
    .chip { font-size:12px; padding:6px 8px; border-radius:10px; }
    .hint { padding:0 12px 20px; }
  }
</style>
</head>
<body>
<header>
  <h1>Singapore wind &ndash; past 24 hours</h1>
  <div class="sub" id="sub"></div>
</header>
<div class="zoombar">
  <button id="zout" type="button" aria-label="Zoom out">&minus;</button>
  <button id="zin" type="button" aria-label="Zoom in">+</button>
  <button data-hours="3" type="button">3h</button>
  <button data-hours="6" type="button">6h</button>
  <button data-hours="12" type="button">12h</button>
  <button data-hours="24" type="button">24h</button>
</div>
<div class="lockbar" id="lockbar" hidden><button id="lock" type="button"></button></div>
<div id="chart"></div>
<details class="panel" id="stationPanel" open>
  <summary>Stations</summary>
  <div class="bar">
    <button id="all">Show all</button>
    <button id="none">Hide all</button>
    <button id="reset">Reset zoom</button>
  </div>
  <div id="chips"></div>
</details>
<div class="hint" id="hint"></div>
<script>
const STORE = __DATA__;
if (typeof Plotly === "undefined") {
  document.getElementById("chart").textContent = "Chart library failed to load.";
  throw new Error("Plotly missing");
}
const times = Object.keys(STORE.readings).sort();
const rank = id => STORE.regions.indexOf(STORE.stations[id].region);
const ids = Object.keys(STORE.stations).sort((a,b) =>
  rank(a) - rank(b) || STORE.stations[a].name.localeCompare(STORE.stations[b].name));
const COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
const compass = d => COMPASS[Math.round(d / 22.5) % 16];
const sgt = t => t.replace("T", " ");   // stored as SGT wall-clock
const palette = ids.map((_, i) => `hsl(${(i * 137.5) % 360} 65% 48%)`);
const touch = matchMedia("(pointer: coarse)").matches;
const narrow = () => innerWidth <= 640;
let unlocked = false;   // touch only: chart gestures are off until unlocked so the page can scroll

const speedTraces = [], dirTraces = [];
ids.forEach((id, i) => {
  const st = STORE.stations[id];
  const sp = times.map(t => (STORE.readings[t].s || {})[id] ?? null);
  const dr = times.map(t => (STORE.readings[t].d || {})[id] ?? null);
  const common = { name: st.name, showlegend: false, hoverlabel: { namelength: -1 } };
  speedTraces.push({ ...common, x: times, y: sp, type: "scatter", mode: "lines",
    connectgaps: false, line: { color: palette[i], width: 1.6 }, xaxis: "x", yaxis: "y",
    hovertemplate: "<b>%{fullData.name}</b> (" + st.region + ")<br>%{x|%H:%M}<br>%{y:.1f} km/h<extra></extra>" });
  dirTraces.push({ ...common, x: times, y: dr, type: "scatter", mode: "markers",
    marker: { color: palette[i], size: 5 }, xaxis: "x2", yaxis: "y2",
    customdata: dr.map(d => d == null ? "" : compass(d)),
    hovertemplate: "<b>%{fullData.name}</b> (" + st.region + ")<br>%{x|%H:%M}<br>%{y}° (%{customdata})<extra></extra>" });
});

// Vector-mean wind per region and time step: average the u/v components of
// every station reporting both speed and direction, then convert back.
const REGION_COLORS = { North: "#d62728", West: "#1f4e9c", Central: "#8e24aa", East: "#e08a00", South: "#0b8f6a" };
const activeRegions = STORE.regions.filter(r => ids.some(id => STORE.stations[id].region === r));
const regionAvg = {};
activeRegions.forEach(r => {
  const members = ids.filter(id => STORE.stations[id].region === r);
  const sp = [], dr = [], cnt = [];
  times.forEach(t => {
    const S = STORE.readings[t].s || {}, D = STORE.readings[t].d || {};
    let u = 0, v = 0, n = 0;
    members.forEach(id => {
      if (S[id] == null || D[id] == null) return;
      const rad = D[id] * Math.PI / 180;
      u += -S[id] * Math.sin(rad); v += -S[id] * Math.cos(rad); n++;
    });
    if (!n) { sp.push(null); dr.push(null); cnt.push(0); return; }
    u /= n; v /= n;
    sp.push(Math.round(Math.hypot(u, v) * 10) / 10);
    dr.push(Math.round((Math.atan2(-u, -v) * 180 / Math.PI + 360) % 360) % 360);
    cnt.push(n);
  });
  regionAvg[r] = { sp, dr, cnt };
});
const avgSpeedTraces = activeRegions.map(r => ({
  name: r + " average", showlegend: false, x: times, y: regionAvg[r].sp, customdata: regionAvg[r].cnt,
  type: "scatter", mode: "lines", connectgaps: false, xaxis: "x", yaxis: "y",
  line: { color: REGION_COLORS[r], width: 3.5 },
  hovertemplate: "<b>" + r + " average</b> (vector mean of %{customdata} stations)<br>%{x|%H:%M}<br>%{y:.1f} km/h<extra></extra>" }));
const avgDirTraces = activeRegions.map(r => ({
  name: r + " average", showlegend: false, x: times, y: regionAvg[r].dr,
  customdata: regionAvg[r].dr.map((d, k) => d == null ? "" : compass(d) + " · " + regionAvg[r].cnt[k] + " stn"),
  type: "scatter", mode: "markers", xaxis: "x2", yaxis: "y2",
  marker: { color: REGION_COLORS[r], size: 8, symbol: "diamond", line: { color: "#fff", width: 1 } },
  hovertemplate: "<b>" + r + " average</b><br>%{x|%H:%M}<br>%{y}° (%{customdata})<extra></extra>" }));

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
// Settings that depend on viewport width or colour scheme; safe to re-apply
// without disturbing the user's zoom.
function adaptive() {
  const n = narrow(), fg = css("--fg"), grid = css("--line");
  const note = (y, text) => ({ xref: "paper", yref: "paper", x: 0, y, xanchor: "left", yanchor: "bottom",
    showarrow: false, text, font: { size: n ? 11 : 12, color: css("--muted") } });
  return {
    paper_bgcolor: css("--bg"), plot_bgcolor: css("--bg"),
    "font.color": fg, "font.size": n ? 10 : 12,
    "xaxis.gridcolor": grid, "xaxis2.gridcolor": grid, "yaxis.gridcolor": grid, "yaxis2.gridcolor": grid,
    "yaxis.linecolor": grid, "yaxis2.linecolor": grid, "xaxis2.linecolor": grid,
    margin: n ? { l: 34, r: 8, t: 24, b: 38 } : { l: 56, r: 20, t: 24, b: 44 },
    "yaxis2.ticktext": n ? ["N", "E", "S", "W", "N"] : ["N 0°", "E 90°", "S 180°", "W 270°", "N 360°"],
    "xaxis2.tickformat": n ? "%H:%M" : "%H:%M<br>%d %b",
    "xaxis2.nticks": n ? 5 : 10,
    dragmode: touch ? (unlocked ? "pan" : false) : "zoom",
    annotations: [note(1, "Wind speed (km/h)"), note(0.455, "Wind direction, degrees the wind comes from")],
  };
}
const base = {
  margin: {},
  grid: { rows: 2, columns: 1, pattern: "independent", roworder: "top to bottom" },
  xaxis:  { type: "date", anchor: "y",  domain: [0, 1], matches: "x2", showticklabels: false },
  xaxis2: { type: "date", anchor: "y2", domain: [0, 1], rangeslider: { visible: false } },
  yaxis:  { domain: [0.54, 1], rangemode: "tozero" },
  yaxis2: { domain: [0, 0.43], range: [0, 360], tickmode: "array", tickvals: [0, 90, 180, 270, 360] },
  hovermode: "closest", hoverdistance: 30, showlegend: false,
};
const config = { responsive: true, displaylogo: false, scrollZoom: !touch, displayModeBar: !touch,
                 modeBarButtonsToRemove: ["lasso2d", "select2d", "autoScale2d"] };
const el = document.getElementById("chart");
const initial = adaptive();
const layoutInit = { ...base, ...Object.fromEntries(Object.entries(initial).filter(([k]) => !k.includes("."))) };
for (const [k, v] of Object.entries(initial)) if (k.includes(".")) {
  const [o, f] = k.split("."); layoutInit[o] = { ...(layoutInit[o] || {}), [f]: v };
}
Plotly.newPlot(el, [...speedTraces, ...dirTraces, ...avgSpeedTraces, ...avgDirTraces], layoutInit, config);

if (touch) {
  const lb = document.getElementById("lock"), bar = document.getElementById("lockbar");
  bar.hidden = false;
  const label = () => {
    lb.textContent = unlocked ? "Chart unlocked: drag to pan. Tap to lock and scroll" : "Tap to unlock dragging the chart sideways";
    lb.classList.toggle("unlocked", unlocked);
  };
  label();
  lb.onclick = () => { unlocked = !unlocked; label(); Plotly.relayout(el, { dragmode: unlocked ? "pan" : false }); };
}


// ── Zoom controls (work on touch, where drag-zoom and pinch are unavailable) ──
const toMs = str => Date.parse(str.replace(" ", "T").slice(0, 23) + "Z");
const toStr = ms => new Date(ms).toISOString().slice(0, 23).replace("T", " ");
const tMin = times.length ? toMs(times[0]) : 0, tMax = times.length ? toMs(times[times.length - 1]) : 0;
const MIN_SPAN = 20 * 60e3, FULL = Math.max(tMax - tMin, MIN_SPAN);
let fitting = false;
function setRange(a, b) {
  const span = Math.min(Math.max(b - a, MIN_SPAN), FULL * 1.02);
  let lo = (a + b) / 2 - span / 2;
  lo = Math.min(Math.max(lo, tMin - FULL * 0.01), tMax + FULL * 0.01 - span);
  Plotly.relayout(el, { "xaxis2.range": [toStr(lo), toStr(lo + span)] });
}
function curRange() { return el._fullLayout.xaxis2.range.map(toMs); }
function zoomBy(f) { const [a, b] = curRange(), c = (a + b) / 2; setRange(c - (c - a) * f, c + (b - c) * f); }
document.getElementById("zin").onclick  = () => zoomBy(0.5);
document.getElementById("zout").onclick = () => zoomBy(2);
document.querySelectorAll(".zoombar [data-hours]").forEach(b => b.onclick = () => {
  const h = +b.dataset.hours;
  if (h >= 24) { Plotly.relayout(el, { "xaxis2.autorange": true }); return; }
  setRange(tMax - h * 3600e3, tMax + FULL * 0.01);
});

// Rescale the speed axis to the visible time window and visible traces.
function fitY() {
  if (fitting || !times.length) return;
  const [a, b] = curRange();
  let max = 0;
  const visibleIdx = [...on.map((v, i) => v ? i : -1), ...avgOn.map((v, i) => v ? ids.length * 2 + i : -1)].filter(i => i >= 0);
  const data = el.data;
  visibleIdx.forEach(i => data[i].y.forEach((y, k) => {
    if (y == null) return;
    const t = toMs(times[k]);
    if (t >= a && t <= b && y > max) max = y;
  }));
  if (!max) return;
  fitting = true;
  Plotly.relayout(el, { "yaxis.range": [0, Math.ceil(max * 1.1)] }).then(() => { fitting = false; }, () => { fitting = false; });
}
el.on("plotly_relayout", ev => {
  if (fitting) return;
  const keys = Object.keys(ev);
  if (keys.some(k => k.startsWith("xaxis2.range") || k === "xaxis2.autorange" || k === "xaxis.autorange")) {
    if (keys.includes("xaxis2.autorange") || keys.includes("xaxis.autorange")) {
      fitting = true;
      Plotly.relayout(el, { "yaxis.autorange": true }).then(() => { fitting = false; }, () => { fitting = false; });
    } else fitY();
  }
});

let timer;
const refresh = () => { clearTimeout(timer); timer = setTimeout(() => Plotly.relayout(el, adaptive()), 150); };
addEventListener("resize", refresh);
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", refresh);

// Station chips (replace the Plotly legend, which does not fit on phones).
const on = ids.map(() => true);
const avgOn = activeRegions.map(() => true);
const avgEls = [];
const chips = document.getElementById("chips");
const chipEls = [];
STORE.regions.forEach(region => {
  const members = ids.map((id, i) => i).filter(i => STORE.stations[ids[i]].region === region);
  if (!members.length) return;
  const g = document.createElement("div"); g.className = "group";
  const head = document.createElement("div"); head.className = "ghead";
  head.innerHTML = `<h3>${region} <span style="color:var(--muted);font-weight:400">(${members.length})</span></h3>`;
  const only = document.createElement("button"); only.type = "button"; only.textContent = "Only";
  only.onclick = () => { on.fill(false); avgOn.fill(false); members.forEach(i => on[i] = true);
    avgOn[activeRegions.indexOf(region)] = true; apply(); };
  const tog = document.createElement("button"); tog.type = "button"; tog.textContent = "Toggle";
  tog.onclick = () => { const all = members.every(i => on[i]); members.forEach(i => on[i] = !all); apply(); };
  head.append(only, tog);
  const grid = document.createElement("div"); grid.className = "chips";
  const ai = activeRegions.indexOf(region);
  const ab = document.createElement("button");
  ab.className = "chip avg"; ab.type = "button";
  ab.innerHTML = `<i style="background:${REGION_COLORS[region]}"></i><span>${region} average (vector mean)</span>`;
  ab.onclick = () => { avgOn[ai] = !avgOn[ai]; apply(); };
  grid.appendChild(ab); avgEls[ai] = ab;
  members.forEach(i => {
    const b = document.createElement("button");
    b.className = "chip"; b.type = "button";
    b.innerHTML = `<i style="background:${palette[i]}"></i><span></span>`;
    b.lastChild.textContent = STORE.stations[ids[i]].name;
    b.onclick = () => { on[i] = !on[i]; apply(); };
    grid.appendChild(b); chipEls[i] = b;
  });
  g.append(head, grid); chips.appendChild(g);
});
function apply() {
  chipEls.forEach((b, i) => b.classList.toggle("off", !on[i]));
  avgEls.forEach((b, i) => b.classList.toggle("off", !avgOn[i]));
  const vis = [...on, ...on, ...avgOn, ...avgOn];
  Plotly.restyle(el, { visible: vis }).then(fitY);
}
document.getElementById("all").onclick  = () => { on.fill(true);  avgOn.fill(true);  apply(); };
document.getElementById("none").onclick = () => { on.fill(false); avgOn.fill(false); apply(); };
document.getElementById("reset").onclick = () =>
  Plotly.relayout(el, { "xaxis.autorange": true, "xaxis2.autorange": true, "yaxis.autorange": true, "yaxis2.range": [0, 360] });
if (narrow()) document.getElementById("stationPanel").open = false;

document.getElementById("hint").innerHTML = touch
  ? "Swipe scrolls the page. Zoom with the &minus;/+ and 3h/6h/12h/24h buttons, then unlock the chart to drag it sideways; tap a line or dot for exact values. Thick lines and diamonds are each region's vector-mean wind. Speed in km/h; direction is the bearing the wind blows <em>from</em>."
  : "Drag to zoom, scroll to zoom, shift+drag to pan, double-click to reset. Thick lines and diamonds are the vector-mean wind of each region. Hover for exact values. Speed in km/h; direction is the bearing the wind blows <em>from</em>.";

const have = times.filter(t => Object.keys(STORE.readings[t].s || {}).length).length;
document.getElementById("sub").textContent =
  `${ids.length} stations · ${have}/${times.length} slots · ` +
  (times.length ? `${sgt(times[0])} → ${sgt(times[times.length - 1])} SGT` : "no data yet");
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
    plotly = (Path(__file__).parent / "vendor" / "plotly-basic.min.js").read_text(encoding="utf-8")
    html = HTML.replace("__DATA__", payload).replace("__PLOTLY__", plotly)
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
