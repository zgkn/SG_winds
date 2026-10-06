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
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; }
  header { padding:16px 16px 4px; }
  h1 { margin:0 0 4px; font-size:20px; }
  .sub { color:var(--muted); font-size:13px; }
  .bar { padding:8px 16px; display:flex; gap:8px; flex-wrap:wrap; }
  button { background:var(--panel); color:var(--fg); border:1px solid var(--line);
           border-radius:6px; padding:5px 10px; cursor:pointer; font:inherit; }
  button:hover { border-color:var(--muted); }
  #chart { width:100%; height:calc(100vh - 150px); min-height:560px; }
  .hint { padding:0 16px 16px; color:var(--muted); font-size:12px; }
</style>
</head>
<body>
<header>
  <h1>Singapore wind &ndash; past 24 hours</h1>
  <div class="sub" id="sub"></div>
</header>
<div class="bar">
  <button id="all">Show all stations</button>
  <button id="none">Hide all</button>
  <button id="reset">Reset zoom</button>
</div>
<div id="chart"></div>
<div class="hint">Drag to zoom, double-click to reset, shift+drag to pan. Click a legend entry to toggle a station,
double-click one to isolate it. Speed in km/h; direction is the bearing the wind blows <em>from</em>.</div>
<script>
const STORE = __DATA__;
if (typeof Plotly === "undefined") {
  document.getElementById("chart").textContent = "Chart library failed to load.";
  throw new Error("Plotly missing");
}
const times = Object.keys(STORE.readings).sort();
const ids = Object.keys(STORE.stations).sort((a,b) => STORE.stations[a].name.localeCompare(STORE.stations[b].name));
const COMPASS = ["N","NNE","NE","ENE","E","ESE","SE","SSE","S","SSW","SW","WSW","W","WNW","NW","NNW"];
const compass = d => COMPASS[Math.round(d / 22.5) % 16];

const sgt = t => t.replace("T", " ");   // stored as SGT wall-clock
const palette = ids.map((_, i) => `hsl(${(i * 137.5) % 360} 65% 48%)`);

const speedTraces = [], dirTraces = [];
ids.forEach((id, i) => {
  const st = STORE.stations[id];
  const sp = times.map(t => (STORE.readings[t].s || {})[id] ?? null);
  const dr = times.map(t => (STORE.readings[t].d || {})[id] ?? null);
  const common = { name: st.name, legendgroup: id, hoverlabel: { namelength: -1 } };
  speedTraces.push({ ...common, x: times, y: sp, type: "scatter", mode: "lines",
    connectgaps: false, line: { color: palette[i], width: 1.6 }, xaxis: "x", yaxis: "y",
    hovertemplate: "<b>%{fullData.name}</b><br>%{x|%H:%M}<br>%{y:.1f} km/h<extra></extra>" });
  dirTraces.push({ ...common, x: times, y: dr, type: "scatter", mode: "markers",
    showlegend: false, marker: { color: palette[i], size: 5 }, xaxis: "x2", yaxis: "y2",
    customdata: dr.map(d => d == null ? "" : compass(d)),
    hovertemplate: "<b>%{fullData.name}</b><br>%{x|%H:%M}<br>%{y}° (%{customdata})<extra></extra>" });
});

const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
function layout() {
  const grid = css("--line"), font = { color: css("--fg") };
  const ax = extra => ({ gridcolor: grid, zerolinecolor: grid, linecolor: grid, ...extra });
  return {
    paper_bgcolor: css("--bg"), plot_bgcolor: css("--bg"), font,
    margin: { l: 70, r: 20, t: 10, b: 40 },
    grid: { rows: 2, columns: 1, pattern: "independent", roworder: "top to bottom" },
    xaxis:  ax({ type: "date", anchor: "y",  domain: [0, 1], matches: "x2", showticklabels: false }),
    xaxis2: ax({ type: "date", anchor: "y2", domain: [0, 1], tickformat: "%H:%M<br>%d %b", rangeslider: { visible: false } }),
    yaxis:  ax({ automargin: true, title: { text: "Wind speed (km/h)", standoff: 8 }, domain: [0.54, 1], rangemode: "tozero", fixedrange: false }),
    yaxis2: ax({ automargin: true, title: { text: "Direction (° from)", standoff: 8 }, domain: [0, 0.46], range: [0, 360],
                 tickmode: "array", tickvals: [0, 90, 180, 270, 360],
                 ticktext: ["N 0°", "E 90°", "S 180°", "W 270°", "N 360°"] }),
    hovermode: "closest", dragmode: "zoom",
    legend: { orientation: "v", font: { size: 11 } },
  };
}
const config = { responsive: true, displaylogo: false, scrollZoom: true,
                 modeBarButtonsToRemove: ["lasso2d", "select2d"] };
const el = document.getElementById("chart");
Plotly.newPlot(el, [...speedTraces, ...dirTraces], layout(), config);
window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change",
  () => Plotly.relayout(el, layout()));

const setVisible = v => Plotly.restyle(el, { visible: v }, [...Array(ids.length * 2).keys()]);
// Plotly toggles legendgroup members together, so only the lines need driving.
document.getElementById("all").onclick  = () => setVisible(true);
document.getElementById("none").onclick = () => setVisible("legendonly");
document.getElementById("reset").onclick = () =>
  Plotly.relayout(el, { "xaxis.autorange": true, "xaxis2.autorange": true, "yaxis.autorange": true, "yaxis2.range": [0, 360] });

const have = times.filter(t => Object.keys(STORE.readings[t].s || {}).length).length;
document.getElementById("sub").textContent =
  `${ids.length} stations · ${have} of ${times.length} time slots with data · ` +
  (times.length ? `${sgt(times[0])} to ${sgt(times[times.length - 1])} SGT` : "no data yet") +
  ` · updated ${STORE.updated} SGT`;
</script>
</body>
</html>
"""


def render(store):
    DOCS.mkdir(exist_ok=True)
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
