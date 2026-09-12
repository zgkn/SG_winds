"""
build.py — Fetch NEA wind data for all stations and render a static website.

Outputs to ./docs/  (served by GitHub Pages).

Environment variables (set by Actions):
  REPORT_DATE   YYYY-MM-DD (SGT)
  HOUR_START    integer 0-23
  HOUR_END      integer 0-23 (inclusive)
  NEA_API_KEY   optional — doubles rate limit from 6 → 12 calls/10 s
"""

import os, sys, time, json, math, base64, io
from datetime import datetime, timedelta
from pathlib import Path

import requests
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
from matplotlib.cm import ScalarMappable
from matplotlib.colors import Normalize

# ── Config ────────────────────────────────────────────────────────────────────

API_SPEED = "https://api-open.data.gov.sg/v2/real-time/api/wind-speed"
API_DIR   = "https://api-open.data.gov.sg/v2/real-time/api/wind-direction"

API_KEY    = os.environ.get("NEA_API_KEY", "")
DATE_STR   = os.environ.get("REPORT_DATE", "2026-09-12")
HOUR_START = int(os.environ.get("HOUR_START", "8"))
HOUR_END   = int(os.environ.get("HOUR_END",   "9"))

# Rate-limit: 6 calls/10 s without key, 12 with key (reset every 10 s)
CALLS_PER_WINDOW = 12 if API_KEY else 6
WINDOW_SECS      = 11        # slightly over 10 s for safety
MAX_RETRIES      = 6
BACKOFF_BASE     = 2

DOCS = Path("docs")
DOCS.mkdir(exist_ok=True)

# ── Known NEA station metadata (id, name, lat, lon) ─────────────────────────
# Sourced from data.gov.sg historical wind datasets and NEA station lists.
# The API returns metadata dynamically; this is used for the map only.
STATION_META = {
    "S50":  ("Clementi Road",           1.3337,  103.7768),
    "S43":  ("Kim Chuan Road",          1.3399,  103.8878),
    "S109": ("Ang Mo Kio Avenue 5",     1.3764,  103.8492),
    "S111": ("Scotts Road",             1.3105,  103.8365),
    "S107": ("Nanyang Avenue",          1.3415,  103.6812),
    "S117": ("Banyan Road",             1.2560,  103.6790),
    "S104": ("Woodlands Avenue 9",      1.4387,  103.7870),
    "S106": ("Pulau Ubin",              1.4042,  103.9675),
    "S108": ("Marina Gardens Drive",    1.2799,  103.8703),
    "S116": ("West Coast Highway",      1.2994,  103.7548),
    "S115": ("Tuas South Avenue 3",     1.2894,  103.6318),
    "S60":  ("Sentosa Island",          1.2494,  103.8303),
    "S44":  ("Buona Vista",             1.3069,  103.7900),
    "S121": ("Jurong Island",           1.2677,  103.7018),
    "S122": ("Ulu Pandan Road",         1.3211,  103.7716),
    "S24":  ("Upper Changi Road North", 1.3678,  103.9829),
    "S40":  ("Tai Seng",                1.3357,  103.8878),  # approximate
}

# ── Fetch helpers ─────────────────────────────────────────────────────────────

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
    """Return {stationId: value} and station_metadata list from one API response."""
    readings = {}
    meta = {}
    if not payload:
        return readings, meta
    try:
        data = payload.get("data", {})
        # Station metadata (lat/lon) comes from the first response
        for s in data.get("stations", []):
            sid = s.get("id") or s.get("stationId")
            if sid:
                loc = s.get("location", s.get("labelLocation", {}))
                meta[sid] = {
                    "name": s.get("name", sid),
                    "lat":  float(loc.get("latitude",  0)),
                    "lon":  float(loc.get("longitude", 0)),
                }
        for block in data.get("readings", []):
            for item in block.get("data", []):
                sid = item.get("stationId") or item.get("station_id")
                val = item.get("value")
                if sid and val is not None:
                    readings[sid] = float(val)
    except (KeyError, TypeError):
        pass
    return readings, meta


# ── Data collection ───────────────────────────────────────────────────────────

def collect():
    """
    Build minute-by-minute time series for all stations.
    Returns:
        timestamps  : list[datetime]
        speed_ts    : dict[stationId -> list[float|nan]]
        dir_ts      : dict[stationId -> list[float|nan]]
        station_info: dict[stationId -> {name, lat, lon}]
    """
    date = datetime.strptime(DATE_STR, "%Y-%m-%d")
    start = date.replace(hour=HOUR_START)
    # Inclusive end hour → fetch through end_hour:59
    end   = date.replace(hour=HOUR_END, minute=59)
    minutes = []
    t = start
    while t <= end:
        minutes.append(t)
        t += timedelta(minutes=1)

    total  = len(minutes)
    print(f"\nFetching {total} minutes × 2 endpoints = {total*2} calls")
    print(f"Rate limit: {CALLS_PER_WINDOW} calls/10s  →  batch every {WINDOW_SECS}s\n")

    speed_ts    = {}
    dir_ts      = {}
    station_info = {}
    timestamps  = []

    # We can fit CALLS_PER_WINDOW / 2 minute-steps per window
    # (2 calls per step: speed + direction)
    batch_size = max(1, CALLS_PER_WINDOW // 2)

    for batch_start in range(0, total, batch_size):
        batch = minutes[batch_start: batch_start + batch_size]

        for i, t in enumerate(batch):
            global_i = batch_start + i
            pct = int((global_i + 1) / total * 40)
            bar = "█" * pct + "░" * (40 - pct)
            print(f"\r  [{bar}] {global_i+1:03d}/{total}  {t:%H:%M}", end="", flush=True)

            sp_payload  = fetch(API_SPEED, t)
            dir_payload = fetch(API_DIR,   t)

            sp_vals,  sp_meta  = extract(sp_payload)
            dir_vals, dir_meta = extract(dir_payload)

            # Merge station metadata from either endpoint
            for sid, info in {**sp_meta, **dir_meta}.items():
                if sid not in station_info:
                    station_info[sid] = info

            # Union of all station IDs seen so far
            all_sids = set(sp_vals) | set(dir_vals) | set(speed_ts)
            timestamps.append(t)

            for sid in all_sids:
                speed_ts.setdefault(sid, [np.nan] * global_i)
                dir_ts.setdefault(sid,   [np.nan] * global_i)
                speed_ts[sid].append(sp_vals.get(sid,  np.nan))
                dir_ts[sid].append(dir_vals.get(sid, np.nan))

        # Sleep between batches (not after the last one)
        if batch_start + batch_size < total:
            print(f"\n  [pause {WINDOW_SECS}s]", end="", flush=True)
            time.sleep(WINDOW_SECS)

    print()

    # Pad any series shorter than timestamps (stations that appeared mid-run)
    n = len(timestamps)
    for sid in speed_ts:
        while len(speed_ts[sid]) < n: speed_ts[sid].append(np.nan)
        while len(dir_ts[sid])   < n: dir_ts[sid].append(np.nan)

    # Fill in known metadata for stations the API didn't return location for
    for sid, (name, lat, lon) in STATION_META.items():
        if sid in speed_ts and sid not in station_info:
            station_info[sid] = {"name": name, "lat": lat, "lon": lon}

    return timestamps, speed_ts, dir_ts, station_info


# ── Chart helpers ─────────────────────────────────────────────────────────────

BG     = "#0d1117"
PANEL  = "#161b22"
GRID   = "#30363d"
TEXT   = "#e6edf3"
MUTED  = "#8b949e"
ACCENT = "#58a6ff"

plt.rcParams.update({
    "figure.facecolor": BG,   "axes.facecolor":   PANEL,
    "axes.edgecolor":   GRID, "axes.labelcolor":  TEXT,
    "axes.titlecolor":  TEXT, "xtick.color":      MUTED,
    "ytick.color":      MUTED,"grid.color":       GRID,
    "grid.linewidth":   0.6,  "text.color":       TEXT,
    "legend.facecolor": PANEL,"legend.edgecolor": GRID,
    "legend.labelcolor":TEXT, "font.family":      "monospace",
    "font.size":        9,
})


def fig_to_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


def ms_to_kmh(v):
    return np.array(v, dtype=float) * 3.6


def vector_mean_wind(speed, direction):
    """
    Average wind speed + direction (met bearing, FROM) by decomposing each
    reading into u/v (eastward/northward) components, averaging those, then
    recombining into a resultant speed and direction. This is the standard
    vector-mean wind: a calm, directionally-erratic minute barely moves the
    average, unlike separately arithmetic-averaging speed and circular-
    averaging direction, where every minute's direction counts equally
    regardless of how weak the wind was.

    Returns (mean_speed, mean_dir); both nan if no valid readings.
    """
    speed = np.array(speed, dtype=float)
    direction = np.array(direction, dtype=float)
    ok = ~np.isnan(speed) & ~np.isnan(direction)
    if not ok.any():
        return np.nan, np.nan
    r = np.radians(direction[ok])
    u = -speed[ok] * np.sin(r)
    v = -speed[ok] * np.cos(r)
    u_mean, v_mean = np.mean(u), np.mean(v)
    mean_speed = math.hypot(u_mean, v_mean)
    mean_dir = math.degrees(math.atan2(-u_mean, -v_mean)) % 360
    return mean_speed, mean_dir


def bearing_arrow(deg):
    """Return (dx, dy) unit vector pointing in met-bearing direction (FROM)."""
    r = math.radians(deg)
    return -math.sin(r), -math.cos(r)


# ── Chart 1: Spaghetti speed time series (all stations) ──────────────────────

def chart_spaghetti(timestamps, speed_ts):
    sids = sorted(speed_ts)
    cmap = matplotlib.colormaps["tab20"].resampled(len(sids))
    t_arr = np.array(timestamps)

    fig, ax = plt.subplots(figsize=(14, 5))
    fig.patch.set_facecolor(BG)

    for i, sid in enumerate(sids):
        spd = ms_to_kmh(speed_ts[sid])
        ax.plot(t_arr, spd, color=cmap(i), lw=1.2, alpha=0.8,
                label=sid)

    ax.set_title(f"Wind Speed — All Stations  |  {DATE_STR}  {HOUR_START:02d}:00–{HOUR_END:02d}:59 SGT",
                 fontsize=11, pad=8)
    ax.set_ylabel("km/h")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M"))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60*(HOUR_END-HOUR_START+1), 5)))
    ax.tick_params(axis="x", rotation=45, labelsize=8)
    ax.legend(ncol=4, fontsize=7, loc="upper right",
              framealpha=0.7, handlelength=1.2)
    ax.grid(True, ls="--", alpha=0.4)
    ax.set_xlabel("Time (SGT)")
    return fig_to_b64(fig)


# ── Chart 2: Heatmap — station × time (speed) ────────────────────────────────

def chart_heatmap(timestamps, speed_ts, station_info):
    sids = sorted(speed_ts, key=lambda s: station_info.get(s, {}).get("name", s))
    n_time = len(timestamps)
    matrix = np.full((len(sids), n_time), np.nan)
    for row, sid in enumerate(sids):
        matrix[row] = ms_to_kmh(speed_ts[sid])

    fig, ax = plt.subplots(figsize=(14, max(4, len(sids) * 0.45)))
    fig.patch.set_facecolor(BG)

    im = ax.imshow(matrix, aspect="auto", cmap="YlOrRd",
                   vmin=0, vmax=np.nanpercentile(matrix, 97),
                   interpolation="nearest")

    # x-axis: every 5 minutes
    step = max(1, n_time // 12)
    ax.set_xticks(range(0, n_time, step))
    ax.set_xticklabels([timestamps[i].strftime("%H:%M") for i in range(0, n_time, step)],
                       rotation=45, fontsize=7)

    # y-axis: station names
    labels = [station_info.get(sid, {}).get("name", sid) + f" ({sid})" for sid in sids]
    ax.set_yticks(range(len(sids)))
    ax.set_yticklabels(labels, fontsize=8)

    cb = fig.colorbar(im, ax=ax, fraction=0.025, pad=0.01)
    cb.set_label("km/h", color=TEXT)
    cb.ax.yaxis.set_tick_params(color=MUTED, labelcolor=MUTED)

    ax.set_title(f"Speed Heatmap — All Stations  |  {DATE_STR} SGT", fontsize=11, pad=8)
    fig.tight_layout()
    return fig_to_b64(fig)


# ── Chart 3: Station ranking (mean speed + direction arrow) ──────────────────

def chart_ranking(speed_ts, dir_ts, station_info):
    rows = []
    for sid, spd_raw in speed_ts.items():
        spd = ms_to_kmh(spd_raw)
        valid_spd = spd[~np.isnan(spd)]
        if len(valid_spd) == 0: continue
        dirs = np.array(dir_ts.get(sid, []), dtype=float)
        mean_spd, mean_dir = vector_mean_wind(spd, dirs)
        rows.append({
            "sid":  sid,
            "name": station_info.get(sid, {}).get("name", sid),
            "mean": mean_spd,
            "max":  float(np.nanmax(spd)),
            "dir":  mean_dir,
        })
    rows.sort(key=lambda r: r["mean"], reverse=True)

    fig, ax = plt.subplots(figsize=(10, max(4, len(rows) * 0.45)))
    fig.patch.set_facecolor(BG)

    y     = range(len(rows))
    means = [r["mean"] for r in rows]
    maxes = [r["max"]  for r in rows]
    cmap  = matplotlib.colormaps["YlOrRd"]
    norm  = Normalize(vmin=0, vmax=max(maxes) if maxes else 1)

    ax.barh(list(y), maxes,  color=[cmap(norm(m)*0.5+0.15) for m in maxes],
            alpha=0.35, label="max")
    ax.barh(list(y), means,  color=[cmap(norm(m)) for m in means],
            alpha=0.9, label="mean")

    # Direction arrows
    for yi, row in enumerate(rows):
        if not np.isnan(row["dir"]):
            dx, dy = bearing_arrow(row["dir"])
            ax.annotate("", xy=(row["mean"] + dx*1.5, yi + dy*0.25),
                        xytext=(row["mean"], yi),
                        arrowprops=dict(arrowstyle="->", color=ACCENT, lw=1.2))

    labels = [f"{r['name']} ({r['sid']})" for r in rows]
    ax.set_yticks(list(y))
    ax.set_yticklabels(labels, fontsize=8)
    ax.set_xlabel("km/h")
    ax.set_title(f"Station Rankings (mean speed) — {DATE_STR} SGT", fontsize=11, pad=8)
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, ls="--", alpha=0.4, axis="x")
    ax.invert_yaxis()
    fig.tight_layout()
    return fig_to_b64(fig)


# ── Chart 4: Polar wind roses — small multiples ──────────────────────────────

def chart_roses(speed_ts, dir_ts, station_info):
    sids = [s for s in sorted(speed_ts)
            if np.any(~np.isnan(np.array(dir_ts.get(s, [np.nan]), dtype=float)))]
    n = len(sids)
    cols = 4
    rows = math.ceil(n / cols)

    fig = plt.figure(figsize=(cols * 3.2, rows * 3.0))
    fig.patch.set_facecolor(BG)

    bin_edges = np.arange(0, 361, 20)   # 18 directional bins of 20°

    for i, sid in enumerate(sids):
        ax = fig.add_subplot(rows, cols, i + 1, projection="polar")
        ax.set_facecolor(PANEL)
        ax.set_theta_zero_location("N")
        ax.set_theta_direction(-1)
        ax.tick_params(colors=MUTED, labelsize=6)
        ax.grid(color=GRID, lw=0.5)

        spd  = ms_to_kmh(np.array(speed_ts[sid], dtype=float))
        dirs = np.array(dir_ts.get(sid, []), dtype=float)
        ok   = ~np.isnan(spd) & ~np.isnan(dirs)

        if ok.any():
            # Bin by direction, colour by mean speed in bin
            counts, _ = np.histogram(dirs[ok], bins=bin_edges)
            bin_spd   = []
            for j in range(len(bin_edges) - 1):
                mask = ok & (dirs >= bin_edges[j]) & (dirs < bin_edges[j+1])
                bin_spd.append(float(np.nanmean(spd[mask])) if mask.any() else 0)

            theta   = np.radians((bin_edges[:-1] + bin_edges[1:]) / 2)
            width   = np.radians(18)
            cmap    = matplotlib.colormaps["YlOrRd"]
            max_spd = max(bin_spd) if max(bin_spd) > 0 else 1
            colors  = [cmap(s / max_spd) for s in bin_spd]
            ax.bar(theta, counts, width=width, color=colors, alpha=0.85, align="center")

        name = station_info.get(sid, {}).get("name", sid)
        ax.set_title(f"{name}\n({sid})", fontsize=7, pad=4, color=TEXT)

    fig.suptitle(f"Wind Roses — All Stations  |  {DATE_STR} SGT",
                 fontsize=11, y=1.01, color=TEXT)
    fig.tight_layout()
    return fig_to_b64(fig)


# ── Map: pannable Leaflet map of Singapore ───────────────────────────────────

def build_map_data(speed_ts, dir_ts, station_info):
    """
    Build the per-station records the Leaflet map renders client-side.
    Bubble size/colour = mean speed, arrow = mean wind direction (where FROM).
    """
    rows = []
    for sid, spd_raw in speed_ts.items():
        info = station_info.get(sid)
        if not info or info["lat"] == 0: continue
        spd = ms_to_kmh(spd_raw)
        valid_spd = spd[~np.isnan(spd)]
        if len(valid_spd) == 0: continue
        dirs = np.array(dir_ts.get(sid, []), dtype=float)
        mean_spd, mean_dir = vector_mean_wind(spd, dirs)
        if np.isnan(mean_spd): continue  # no minute had both a speed and a direction reading
        rows.append({
            "sid":      sid,
            "name":     info["name"],
            "lat":      info["lat"],
            "lon":      info["lon"],
            "mean_spd": round(mean_spd, 2),
            "mean_dir": round(mean_dir, 1),
        })
    return rows


# ── Summary table ─────────────────────────────────────────────────────────────

def build_table(speed_ts, dir_ts, station_info):
    rows = []
    for sid, spd_raw in speed_ts.items():
        spd  = ms_to_kmh(spd_raw)
        dirs = np.array(dir_ts.get(sid, []), dtype=float)
        vs   = spd[~np.isnan(spd)]
        if len(vs) == 0: continue
        mean_spd, mean_dir = vector_mean_wind(spd, dirs)
        rows.append({
            "sid":     sid,
            "name":    station_info.get(sid, {}).get("name", "—"),
            "mean":    f"{mean_spd:.1f}" if not np.isnan(mean_spd) else "—",
            "max":     f"{np.nanmax(spd):.1f}",
            "min":     f"{np.nanmin(spd):.1f}",
            "dir":     f"{mean_dir:.0f}°" if not np.isnan(mean_dir) else "—",
            "missing": int(np.sum(np.isnan(spd))),
            "n":       len(spd),
        })
    rows.sort(key=lambda r: float(r["mean"]) if r["mean"] != "—" else -1.0, reverse=True)

    tr = "\n".join(
        f"""<tr>
          <td>{r['sid']}</td><td>{r['name']}</td>
          <td class="num">{r['mean']}</td>
          <td class="num">{r['max']}</td>
          <td class="num">{r['min']}</td>
          <td class="num">{r['dir']}</td>
          <td class="num">{r['missing']}/{r['n']}</td>
        </tr>""" for r in rows)
    return tr


# ── HTML template ─────────────────────────────────────────────────────────────

HTML = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SG Wind Report — {date} {h_start}h–{h_end}h SGT</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#0d1117;color:#e6edf3;font-family:'Courier New',monospace;
        font-size:14px;line-height:1.6;padding:0 0 60px}}
  header{{background:#161b22;border-bottom:1px solid #30363d;
          padding:20px 32px;position:sticky;top:0;z-index:10}}
  header h1{{font-size:1.2rem;font-weight:700;color:#58a6ff}}
  header p{{color:#8b949e;font-size:.85rem;margin-top:4px}}
  nav{{display:flex;gap:16px;margin-top:12px;flex-wrap:wrap}}
  nav a{{color:#8b949e;text-decoration:none;font-size:.8rem;
          padding:3px 8px;border:1px solid #30363d;border-radius:4px}}
  nav a:hover{{color:#e6edf3;border-color:#58a6ff}}
  main{{max-width:1200px;margin:0 auto;padding:32px 24px}}
  section{{margin-bottom:48px}}
  h2{{font-size:1rem;color:#8b949e;text-transform:uppercase;
       letter-spacing:.08em;margin-bottom:16px;padding-bottom:6px;
       border-bottom:1px solid #30363d}}
  .chart-wrap{{background:#161b22;border:1px solid #30363d;
               border-radius:8px;overflow:hidden;padding:8px}}
  .chart-wrap img{{width:100%;height:auto;display:block}}
  .map-wrap{{background:#161b22;border:1px solid #30363d;
             border-radius:8px;overflow:hidden}}
  #leaflet-map{{height:520px;width:100%;background:#0d1117}}
  .wind-arrow{{color:#58a6ff;font-size:18px;line-height:20px;text-align:center;
               text-shadow:0 0 3px #000;pointer-events:none}}
  .leaflet-popup-content-wrapper{{background:#161b22;color:#e6edf3;
               border:1px solid #30363d;font-family:'Courier New',monospace}}
  .leaflet-popup-tip{{background:#161b22}}
  .leaflet-container a.leaflet-popup-close-button{{color:#8b949e}}
  .leaflet-control-zoom a{{background:#161b22;color:#e6edf3;border-color:#30363d}}
  .leaflet-control-attribution{{background:rgba(22,27,34,.8);color:#8b949e}}
  .leaflet-control-attribution a{{color:#58a6ff}}
  table{{width:100%;border-collapse:collapse;font-size:.82rem}}
  th{{background:#1f2937;color:#8b949e;padding:8px 12px;
      text-align:left;font-weight:600;position:sticky;top:72px}}
  td{{padding:7px 12px;border-bottom:1px solid #21262d}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  tr:hover td{{background:#1c2128}}
  .badge{{display:inline-block;padding:2px 6px;border-radius:3px;
           font-size:.75rem;background:#21262d;color:#8b949e;margin-left:8px}}
  footer{{text-align:center;color:#484f58;font-size:.75rem;margin-top:40px}}
</style>
</head>
<body>
<header>
  <h1>Singapore Wind Report</h1>
  <p>{date} &nbsp;·&nbsp; {h_start:02d}:00–{h_end:02d}:59 SGT &nbsp;·&nbsp;
     {n_stations} stations &nbsp;·&nbsp; {n_minutes} minutes
     <span class="badge">NEA / data.gov.sg</span></p>
  <nav>
    <a href="#map">Map</a>
    <a href="#timeseries">Time Series</a>
    <a href="#heatmap">Heatmap</a>
    <a href="#ranking">Rankings</a>
    <a href="#roses">Wind Roses</a>
    <a href="#table">Data Table</a>
  </nav>
</header>
<main>

<section id="map">
  <h2>Geographic Overview</h2>
  <p style="color:#8b949e;font-size:.82rem;margin-bottom:12px">
    Drag to pan, scroll or use the +/- controls to zoom. Circle size and colour = mean
    wind speed. Arrow = mean wind direction (where FROM).
  </p>
  <div class="map-wrap"><div id="leaflet-map"></div></div>
</section>

<section id="timeseries">
  <h2>Speed Time Series — All Stations</h2>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_spag}" alt="time series"></div>
</section>

<section id="heatmap">
  <h2>Speed Heatmap — Station × Time</h2>
  <p style="color:#8b949e;font-size:.82rem;margin-bottom:12px">
    Darker = faster. Gaps = missing data.
  </p>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_heat}" alt="heatmap"></div>
</section>

<section id="ranking">
  <h2>Station Rankings</h2>
  <p style="color:#8b949e;font-size:.82rem;margin-bottom:12px">
    Bars show mean (solid) and max (translucent). Blue arrow = mean wind direction.
  </p>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_rank}" alt="rankings"></div>
</section>

<section id="roses">
  <h2>Wind Roses — Small Multiples</h2>
  <p style="color:#8b949e;font-size:.82rem;margin-bottom:12px">
    Bar length = frequency. Colour = mean speed in that sector (yellow → red).
  </p>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_rose}" alt="wind roses"></div>
</section>

<section id="table">
  <h2>Summary Table</h2>
  <div style="overflow-x:auto">
  <table>
    <thead><tr>
      <th>Station ID</th><th>Name</th>
      <th style="text-align:right">Mean (km/h)</th>
      <th style="text-align:right">Max (km/h)</th>
      <th style="text-align:right">Min (km/h)</th>
      <th style="text-align:right">Mean Dir</th>
      <th style="text-align:right">Missing</th>
    </tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
  </div>
</section>

</main>
<footer>
  Generated by GitHub Actions · Source: NEA / data.gov.sg (Open Data Licence) ·
  Report date: {date} · Built {built}
</footer>
<script src="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.js"></script>
<script>
(function() {{
  var stations = {map_data};
  var map = L.map('leaflet-map', {{
    center: [1.3521, 103.8198],
    zoom: 11,
    minZoom: 10,
    maxZoom: 17,
    scrollWheelZoom: true
  }});

  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/dark_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19
  }}).addTo(map);

  var maxSpd = 1;
  stations.forEach(function(s) {{ if (s.mean_spd > maxSpd) maxSpd = s.mean_spd; }});

  function speedColor(spd) {{
    var t = spd / maxSpd;
    var r = 255;
    var g = Math.round(255 * (1 - t) * 0.7 + 69 * t);
    var b = Math.round(160 * (1 - t));
    return 'rgb(' + r + ',' + g + ',' + b + ')';
  }}

  stations.forEach(function(s) {{
    var radius = 6 + (s.mean_spd / maxSpd) * 16;
    L.circleMarker([s.lat, s.lon], {{
      radius: radius,
      fillColor: speedColor(s.mean_spd),
      color: '#fff',
      weight: 1,
      fillOpacity: 0.85
    }}).addTo(map).bindPopup(
      '<b>' + s.name + '</b> (' + s.sid + ')<br>' +
      'Mean: ' + s.mean_spd.toFixed(1) + ' km/h' +
      (s.mean_dir !== null ? '<br>Dir: ' + Math.round(s.mean_dir) + '&deg;' : '')
    );

    if (s.mean_dir !== null) {{
      var rot = s.mean_dir - 90;
      var icon = L.divIcon({{
        className: 'wind-arrow',
        html: '<div style="transform:rotate(' + rot + 'deg)">&#10148;</div>',
        iconSize: [20, 20],
        iconAnchor: [10, 10]
      }});
      L.marker([s.lat, s.lon], {{icon: icon, interactive: false}}).addTo(map);
    }}
  }});
}})();
</script>
</body>
</html>
"""


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'═'*60}")
    print(f"  SG Wind Report Builder")
    print(f"  Date: {DATE_STR}  Hours: {HOUR_START:02d}h–{HOUR_END:02d}h SGT")
    print(f"  API key: {'YES' if API_KEY else 'NO (unauthenticated)'}")
    print(f"{'═'*60}\n")

    timestamps, speed_ts, dir_ts, station_info = collect()
    n_stations = len(speed_ts)
    n_minutes  = len(timestamps)
    print(f"\n  Collected {n_stations} stations × {n_minutes} minutes\n")

    print("  Building charts …")
    img_spag = chart_spaghetti(timestamps, speed_ts)
    img_heat = chart_heatmap(timestamps, speed_ts, station_info)
    img_rank = chart_ranking(speed_ts, dir_ts, station_info)
    img_rose = chart_roses(speed_ts, dir_ts, station_info)
    map_stations = build_map_data(speed_ts, dir_ts, station_info)
    map_data = json.dumps(map_stations).replace("</", "<\\/")
    tbl_rows = build_table(speed_ts, dir_ts, station_info)

    html = HTML.format(
        date       = DATE_STR,
        h_start    = HOUR_START,
        h_end      = HOUR_END,
        n_stations = n_stations,
        n_minutes  = n_minutes,
        map_data   = map_data,
        img_spag   = img_spag,
        img_heat   = img_heat,
        img_rank   = img_rank,
        img_rose   = img_rose,
        table_rows = tbl_rows,
        built      = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )

    out = DOCS / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n  ✓ Site written to {out}  ({len(html)//1024} KB)\n")


if __name__ == "__main__":
    main()
