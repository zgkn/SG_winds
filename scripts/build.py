"""
build.py — Fetch NEA wind data for all stations and render a static website.

Outputs to ./docs/  (served by GitHub Pages).

Environment variables (set by Actions):
  START_DATETIME   YYYY-MM-DDTHH:MM (SGT), inclusive
  END_DATETIME     YYYY-MM-DDTHH:MM (SGT), inclusive
  NEA_API_KEY      optional — doubles rate limit from 6 → 12 calls/10 s
"""

import os, sys, re, time, json, math, base64, io
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
from matplotlib.colors import Normalize, TwoSlopeNorm

# ── Config ────────────────────────────────────────────────────────────────────

API_SPEED = "https://api-open.data.gov.sg/v2/real-time/api/wind-speed"
API_DIR   = "https://api-open.data.gov.sg/v2/real-time/api/wind-direction"

DATETIME_FMT = "%Y-%m-%dT%H:%M"

API_KEY  = os.environ.get("NEA_API_KEY", "")
START_DT = datetime.strptime(os.environ.get("START_DATETIME", "2026-09-12T08:00"), DATETIME_FMT)
END_DT   = datetime.strptime(os.environ.get("END_DATETIME",   "2026-09-12T09:00"), DATETIME_FMT)

if END_DT < START_DT:
    sys.exit(f"END_DATETIME ({END_DT}) is before START_DATETIME ({START_DT})")

# Display strings for chart titles / page header / footer.
if START_DT.date() == END_DT.date():
    PERIOD_LABEL = f"{START_DT:%Y-%m-%d}  {START_DT:%H:%M}–{END_DT:%H:%M} SGT"
else:
    PERIOD_LABEL = f"{START_DT:%Y-%m-%d %H:%M} – {END_DT:%Y-%m-%d %H:%M} SGT"

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
    """
    Return ({stationId: value}, station_metadata, reading_unit) from one
    API response. reading_unit is whatever the API itself reports (e.g.
    "knots" or "m/s") via data.readingUnit, or None if absent — used to
    verify the assumption baked into speed_to_kmh() rather than trusting it
    blindly.
    """
    readings = {}
    meta = {}
    reading_unit = None
    if not payload:
        return readings, meta, reading_unit
    try:
        data = payload.get("data", {})
        reading_unit = data.get("readingUnit")
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
    return readings, meta, reading_unit


# ── Data collection ───────────────────────────────────────────────────────────

def collect():
    """
    Build minute-by-minute time series for all stations.
    Returns:
        timestamps  : list[datetime]
        speed_ts    : dict[stationId -> list[float|nan]]
        dir_ts      : dict[stationId -> list[float|nan]]
        station_info: dict[stationId -> {name, lat, lon}]
        units       : dict with "speed"/"direction" -> unit string reported
                      by the API (or None if it never included one). Also
                      updates the module-level SPEED_UNIT_FACTOR to match
                      what the API actually reports, rather than trusting a
                      hardcoded assumption.
    """
    minutes = []
    t = START_DT
    while t <= END_DT:
        minutes.append(t)
        t += timedelta(minutes=1)

    total  = len(minutes)
    print(f"\nFetching {total} minutes × 2 endpoints = {total*2} calls")
    print(f"Rate limit: {CALLS_PER_WINDOW} calls/10s  →  batch every {WINDOW_SECS}s\n")

    speed_ts    = {}
    dir_ts      = {}
    station_info = {}
    timestamps  = []
    units       = {"speed": None, "direction": None}

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

            sp_vals,  sp_meta,  sp_unit  = extract(sp_payload)
            dir_vals, dir_meta, dir_unit = extract(dir_payload)
            if units["speed"] is None and sp_unit:
                units["speed"] = sp_unit
            if units["direction"] is None and dir_unit:
                units["direction"] = dir_unit

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

    print(f"  API-reported units — speed: {units['speed'] or 'not provided'}, "
          f"direction: {units['direction'] or 'not provided'}")

    global SPEED_UNIT_FACTOR
    key = (units["speed"] or "").strip().lower()
    if key in SPEED_UNIT_TO_KMH:
        SPEED_UNIT_FACTOR = SPEED_UNIT_TO_KMH[key]
    elif units["speed"]:
        print(f"  ⚠ WARNING: unrecognized speed unit '{units['speed']}' from the "
              f"API — falling back to knots (x1.852). Add it to SPEED_UNIT_TO_KMH "
              f"in build.py if this is wrong.")
        SPEED_UNIT_FACTOR = SPEED_UNIT_TO_KMH["knots"]
    else:
        print(f"  ⚠ WARNING: API did not report a speed unit — assuming knots "
              f"(x1.852), NEA's confirmed unit as of 2026-09-12.")
    print(f"  Using speed conversion factor: x{SPEED_UNIT_FACTOR} -> km/h")

    return timestamps, speed_ts, dir_ts, station_info, units


# ── Chart helpers ─────────────────────────────────────────────────────────────

BG     = "#ffffff"
PANEL  = "#f6f8fa"
GRID   = "#d0d7de"
TEXT   = "#1f2328"
MUTED  = "#59636e"
ACCENT = "#0969da"

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


def fig_to_b64(fig, extra_artists=None):
    """
    extra_artists: artists (e.g. legends added via ax.add_artist()) that
    savefig's tight bbox calculation won't discover on its own and would
    otherwise clip out of the saved image.
    """
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight",
                bbox_extra_artists=extra_artists,
                facecolor=fig.get_facecolor())
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode()


# Confirmed live (2026-09-12): NEA's real-time wind-speed API reports in
# knots, not m/s — this default matches that. collect() overrides it once
# it has actually read the API's own readingUnit field for this run, so a
# future change in what NEA reports doesn't silently produce wrong numbers.
SPEED_UNIT_TO_KMH = {
    "knot": 1.852, "knots": 1.852, "kt": 1.852, "kts": 1.852,
    "m/s": 3.6, "mps": 3.6, "meter per second": 3.6, "meters per second": 3.6,
    "metre per second": 3.6, "metres per second": 3.6,
    "km/h": 1.0, "kmh": 1.0, "kph": 1.0,
    "kilometer per hour": 1.0, "kilometre per hour": 1.0,
}
SPEED_UNIT_FACTOR = SPEED_UNIT_TO_KMH["knots"]


def speed_to_kmh(v):
    """Convert a raw NEA speed reading to km/h using SPEED_UNIT_FACTOR."""
    return np.array(v, dtype=float) * SPEED_UNIT_FACTOR


def mean_uv(speed, direction):
    """
    Decompose each (speed, direction) reading into u/v (eastward/northward)
    components and average those. Returns (u_mean, v_mean), both nan if no
    minute has both a speed and a direction reading.
    """
    speed = np.array(speed, dtype=float)
    direction = np.array(direction, dtype=float)
    ok = ~np.isnan(speed) & ~np.isnan(direction)
    if not ok.any():
        return np.nan, np.nan
    r = np.radians(direction[ok])
    u = -speed[ok] * np.sin(r)
    v = -speed[ok] * np.cos(r)
    return float(np.mean(u)), float(np.mean(v))


def vector_mean_wind(speed, direction):
    """
    Average wind speed + direction (met bearing, FROM) by decomposing each
    reading into u/v components (see mean_uv), then recombining the averaged
    components back into a resultant speed and direction. This is the
    standard vector-mean wind: a calm, directionally-erratic minute barely
    moves the average, unlike separately arithmetic-averaging speed and
    circular-averaging direction, where every minute's direction counts
    equally regardless of how weak the wind was.

    Returns (mean_speed, mean_dir); both nan if no valid readings.
    """
    u_mean, v_mean = mean_uv(speed, direction)
    if np.isnan(u_mean):
        return np.nan, np.nan
    mean_speed = math.hypot(u_mean, v_mean)
    mean_dir = math.degrees(math.atan2(-u_mean, -v_mean)) % 360
    return mean_speed, mean_dir


def bearing_arrow(deg):
    """
    Return (dx, dy) unit vector — in standard (east=+x, north=+y) axes —
    pointing the way the wind is blowing TOWARD. `deg` is the met-bearing
    the wind blows FROM (standard convention, matching the raw NEA
    reading): a vector aimed at bearing b is (sin b, cos b), and the
    travel direction is (deg + 180), so this simplifies to (-sin, -cos).
    """
    r = math.radians(deg)
    return -math.sin(r), -math.cos(r)


# Common words in NEA station names, abbreviated so chart legends stay compact.
_NAME_ABBREVIATIONS = [
    ("Avenue", "Ave"), ("Boulevard", "Blvd"), ("Highway", "Hwy"),
    ("Gardens", "Gdns"), ("Drive", "Dr"), ("Street", "St"), ("Road", "Rd"),
    ("Upper", "Up"), ("North", "N"), ("South", "S"), ("East", "E"), ("West", "W"),
]


def shorten_name(name, max_len=18):
    """Abbreviate common words in a station name, then hard-truncate if still too long."""
    short = name
    for full, abbr in _NAME_ABBREVIATIONS:
        short = re.sub(rf"\b{full}\b", abbr, short)
    if len(short) > max_len:
        short = short[:max_len - 1].rstrip() + "…"
    return short


# Rough geometric region classifier (Singapore has no single station->region
# lookup that's reliably available from the API), used to group the time
# series legend. SG_CENTER is close to the geographic centroid of the island.
SG_CENTER_LAT, SG_CENTER_LON = 1.3521, 103.8198
SG_CENTRAL_RADIUS_DEG = 0.045
REGION_ORDER = ["North", "East", "South", "West", "Central"]


def region_of(lat, lon):
    dlat = lat - SG_CENTER_LAT
    dlon = lon - SG_CENTER_LON
    if math.hypot(dlat, dlon) < SG_CENTRAL_RADIUS_DEG:
        return "Central"
    if abs(dlat) >= abs(dlon):
        return "North" if dlat > 0 else "South"
    return "East" if dlon > 0 else "West"


# ── Chart 1: Speed time series, one small-multiple panel per region ──────────

def chart_spaghetti(timestamps, speed_ts, station_info):
    t_arr = np.array(timestamps)

    sids_by_region = {r: [] for r in REGION_ORDER}
    for sid in sorted(speed_ts):
        info = station_info.get(sid, {})
        lat = info.get("lat") or SG_CENTER_LAT
        lon = info.get("lon") or SG_CENTER_LON
        sids_by_region[region_of(lat, lon)].append(sid)

    active_regions = [r for r in REGION_ORDER if sids_by_region[r]]
    n = max(len(active_regions), 1)
    ncols = min(3, n)
    nrows = math.ceil(n / ncols)

    fig, axes = plt.subplots(nrows, ncols, figsize=(5.6 * ncols, 3.8 * nrows),
                              sharex=True, sharey=True, squeeze=False)
    fig.patch.set_facecolor(BG)
    axes_flat = axes.flatten()

    for idx, region in enumerate(active_regions):
        ax = axes_flat[idx]
        sids = sids_by_region[region]
        cmap = matplotlib.colormaps["tab10"].resampled(len(sids))
        for i, sid in enumerate(sids):
            spd = speed_to_kmh(speed_ts[sid])
            name = station_info.get(sid, {}).get("name", sid)
            ax.plot(t_arr, spd, color=cmap(i), lw=1.3, alpha=0.85,
                    label=shorten_name(name))

        ax.set_title(region, fontsize=10, fontweight="bold")
        locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
        ax.xaxis.set_major_locator(locator)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(locator))
        ax.tick_params(axis="x", rotation=45, labelsize=7)
        ax.grid(True, ls="--", alpha=0.4)
        ax.legend(fontsize=6.5, loc="upper right", framealpha=0.8, handlelength=1.2)

    # Hide any unused trailing cells (grid may have more slots than regions).
    for j in range(len(active_regions), len(axes_flat)):
        axes_flat[j].axis("off")

    fig.supxlabel("Time (SGT)", fontsize=9)
    fig.supylabel("km/h", fontsize=9)
    fig.suptitle(f"Wind Speed by Region  |  {PERIOD_LABEL}", fontsize=12)
    fig.tight_layout(rect=[0.01, 0.01, 1, 0.94])
    return fig_to_b64(fig)


# ── Chart 2: Station ranking (mean speed + direction arrow) ──────────────────

def chart_ranking(speed_ts, dir_ts, station_info):
    rows = []
    for sid, spd_raw in speed_ts.items():
        spd = speed_to_kmh(spd_raw)
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
    ax.set_title(f"Station Rankings (mean speed) — {PERIOD_LABEL}", fontsize=11, pad=8)
    ax.legend(fontsize=8, loc="lower right")
    ax.grid(True, ls="--", alpha=0.4, axis="x")
    ax.invert_yaxis()
    fig.tight_layout()
    return fig_to_b64(fig)


# ── Map: pannable Leaflet map of Singapore ───────────────────────────────────

def build_map_data(speed_ts, dir_ts, station_info):
    """
    Build the per-station records the Leaflet map renders client-side.
    Bubble size/colour = mean speed, arrow = direction the wind is blowing toward.
    """
    rows = []
    for sid, spd_raw in speed_ts.items():
        info = station_info.get(sid)
        if not info or info["lat"] == 0: continue
        spd = speed_to_kmh(spd_raw)
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


# Grid extent for the convergence field — a bit wider than the station
# spread so the interpolation/overlay covers the whole visible basemap.
CONV_LAT_MIN, CONV_LAT_MAX = 1.16, 1.48
CONV_LON_MIN, CONV_LON_MAX = 103.58, 104.10
CONV_BOUNDS = [[CONV_LAT_MIN, CONV_LON_MIN], [CONV_LAT_MAX, CONV_LON_MAX]]
# Deliberately coarse: fine enough to show spatial structure, coarse enough
# that each grid cell is a visually distinct tile with its own border.
CONV_GRID_N = 25


def build_convergence_field(speed_ts, dir_ts, station_info, grid_n=CONV_GRID_N):
    """
    Interpolate station-mean u/v wind components onto a regular lat/lon
    grid (inverse-distance weighting), then compute the horizontal wind
    convergence of the interpolated field: convergence = -(du/dx + dv/dy).

    Positive convergence = air piling up (favours uplift/showers);
    negative = divergence (air spreading out).

    Returns a 2D array (grid_n x grid_n, row 0 = CONV_LAT_MIN) of
    convergence in units of 1e-4 s^-1, or None if fewer than 3 stations
    have a usable wind vector.
    """
    lats, lons, us, vs = [], [], [], []
    for sid, spd_raw in speed_ts.items():
        info = station_info.get(sid)
        if not info or info["lat"] == 0: continue
        spd = speed_to_kmh(spd_raw)
        dirs = np.array(dir_ts.get(sid, []), dtype=float)
        u, v = mean_uv(spd, dirs)
        if np.isnan(u): continue
        lats.append(info["lat"]); lons.append(info["lon"])
        us.append(u); vs.append(v)

    if len(lats) < 3:
        return None

    lats, lons = np.array(lats), np.array(lons)
    us, vs = np.array(us), np.array(vs)

    grid_lat = np.linspace(CONV_LAT_MIN, CONV_LAT_MAX, grid_n)
    grid_lon = np.linspace(CONV_LON_MIN, CONV_LON_MAX, grid_n)
    glon, glat = np.meshgrid(grid_lon, grid_lat)  # both (grid_n, grid_n)

    # Inverse-distance-weighted interpolation (power=2). Degrees are fine
    # as a distance metric at Singapore's scale/latitude (~0.3° across).
    d2 = ((lats[:, None] - glat.ravel()[None, :]) ** 2 +
          (lons[:, None] - glon.ravel()[None, :]) ** 2)
    d2 = np.maximum(d2, 1e-12)
    w = 1.0 / d2
    u_grid = (w * us[:, None]).sum(axis=0) / w.sum(axis=0)
    v_grid = (w * vs[:, None]).sum(axis=0) / w.sum(axis=0)
    u_grid = u_grid.reshape(glat.shape)
    v_grid = v_grid.reshape(glat.shape)

    # Degree spacing -> metres, so the gradient is a physically-scaled s^-1 rate.
    mean_lat_rad = math.radians(np.mean(lats))
    dx_m = (grid_lon[1] - grid_lon[0]) * 111_320 * math.cos(mean_lat_rad)
    dy_m = (grid_lat[1] - grid_lat[0]) * 110_540

    du_dx = np.gradient(u_grid / 3.6, dx_m, axis=1)  # km/h -> m/s first
    dv_dy = np.gradient(v_grid / 3.6, dy_m, axis=0)
    convergence = -(du_dx + dv_dy) * 1e4  # x1e-4 s^-1, a typical display scale
    return convergence


def render_convergence_overlay(convergence):
    """
    Render the convergence field as a transparent PNG for a Leaflet image
    overlay: a mosaic of visibly-separated grid cells (not a smoothed
    blob), so the interpolation grid itself is legible. Red = convergence,
    blue = divergence. Colour always spans the full range of that hour's
    field (TwoSlopeNorm rescales to the field's own min/max each time), and
    opacity is a fixed constant rather than scaled by magnitude — so the
    overlay stays visible even in a calm hour with only weak convergence.
    """
    n_lat, n_lon = convergence.shape
    grid_lat = np.linspace(CONV_LAT_MIN, CONV_LAT_MAX, n_lat)
    grid_lon = np.linspace(CONV_LON_MIN, CONV_LON_MAX, n_lon)
    glon, glat = np.meshgrid(grid_lon, grid_lat)

    fig = plt.figure(figsize=(6, 6), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.axis("off")
    fig.patch.set_alpha(0)

    vmax = max(float(np.nanmax(np.abs(convergence))), 1e-6)
    norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
    cmap = matplotlib.colormaps["RdBu_r"]

    # pcolormesh renders each grid cell as a flat, distinct tile (unlike
    # imshow's smooth interpolation), and edgecolors draws a visible border
    # around every cell so the grid structure itself is clearly visible.
    ax.pcolormesh(glon, glat, convergence, cmap=cmap, norm=norm,
                  shading="nearest", alpha=0.65,
                  edgecolors="#33415560", linewidth=0.6)
    ax.set_xlim(CONV_LON_MIN, CONV_LON_MAX)
    ax.set_ylim(CONV_LAT_MIN, CONV_LAT_MAX)
    ax.set_aspect("auto")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", transparent=True)
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode(), vmax


# ── Summary table ─────────────────────────────────────────────────────────────

def build_table(speed_ts, dir_ts, station_info):
    rows = []
    for sid, spd_raw in speed_ts.items():
        spd  = speed_to_kmh(spd_raw)
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
<title>SG Wind Report — {period_label}</title>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/leaflet.min.css">
<style>
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{background:#ffffff;color:#1f2328;font-family:'Courier New',monospace;
        font-size:14px;line-height:1.6;padding:0 0 60px}}
  header{{background:#f6f8fa;border-bottom:1px solid #d0d7de;
          padding:20px 32px;position:sticky;top:0;z-index:10}}
  header h1{{font-size:1.2rem;font-weight:700;color:#0969da}}
  header p{{color:#59636e;font-size:.85rem;margin-top:4px}}
  nav{{display:flex;gap:16px;margin-top:12px;flex-wrap:wrap}}
  nav a{{color:#59636e;text-decoration:none;font-size:.8rem;
          padding:3px 8px;border:1px solid #d0d7de;border-radius:4px}}
  nav a:hover{{color:#1f2328;border-color:#0969da}}
  main{{max-width:1200px;margin:0 auto;padding:32px 24px}}
  section{{margin-bottom:48px}}
  h2{{font-size:1rem;color:#59636e;text-transform:uppercase;
       letter-spacing:.08em;margin-bottom:16px;padding-bottom:6px;
       border-bottom:1px solid #d0d7de}}
  .chart-wrap{{background:#f6f8fa;border:1px solid #d0d7de;
               border-radius:8px;overflow:hidden;padding:8px}}
  .chart-wrap img{{width:100%;height:auto;display:block}}
  .map-wrap{{background:#f6f8fa;border:1px solid #d0d7de;
             border-radius:8px;overflow:hidden}}
  #leaflet-map{{height:520px;width:100%;background:#f6f8fa}}
  .wind-arrow{{color:#0969da;font-size:18px;line-height:20px;text-align:center;
               text-shadow:0 0 3px #fff;pointer-events:none}}
  .leaflet-popup-content-wrapper{{background:#ffffff;color:#1f2328;
               border:1px solid #d0d7de;font-family:'Courier New',monospace}}
  .leaflet-popup-tip{{background:#ffffff}}
  .leaflet-container a.leaflet-popup-close-button{{color:#59636e}}
  .leaflet-control-zoom a{{background:#ffffff;color:#1f2328;border-color:#d0d7de}}
  .leaflet-control-attribution{{background:rgba(255,255,255,.85);color:#59636e}}
  .leaflet-control-attribution a{{color:#0969da}}
  .conv-scale{{max-width:420px;margin-bottom:16px}}
  .conv-gradient{{height:14px;border-radius:3px;border:1px solid #d0d7de;
    background:linear-gradient(to right,#053061,#2166ac,#4393c3,#92c5de,#d1e5f0,
                #f7f7f7,#fddbc7,#f4a582,#d6604d,#b2182b,#67001f)}}
  .conv-scale-labels{{display:flex;justify-content:space-between;font-size:.72rem;
                       color:#59636e;margin-top:3px}}
  .conv-scale-caption{{font-size:.72rem;color:#59636e;margin-top:2px}}
  .table-scroll{{max-height:480px;overflow:auto;border:1px solid #d0d7de;
                 border-radius:8px}}
  table{{width:100%;border-collapse:collapse;font-size:.82rem}}
  th{{background:#eaeef2;color:#59636e;padding:8px 12px;
      text-align:left;font-weight:600;position:sticky;top:0}}
  td{{padding:7px 12px;border-bottom:1px solid #eaeef2}}
  td.num{{text-align:right;font-variant-numeric:tabular-nums}}
  tr:hover td{{background:#f6f8fa}}
  .badge{{display:inline-block;padding:2px 6px;border-radius:3px;
           font-size:.75rem;background:#eaeef2;color:#59636e;margin-left:8px}}
  footer{{text-align:center;color:#6e7781;font-size:.75rem;margin-top:40px}}
</style>
</head>
<body>
<header>
  <h1>Singapore Wind Report</h1>
  <p>{period_label} &nbsp;·&nbsp;
     {n_stations} stations &nbsp;·&nbsp; {n_minutes} minutes
     <span class="badge">NEA / data.gov.sg</span></p>
  <nav>
    <a href="#map">Map</a>
    <a href="#timeseries">Time Series</a>
    <a href="#ranking">Rankings</a>
    <a href="#table">Data Table</a>
  </nav>
</header>
<main>

<section id="map">
  <h2>Geographic Overview</h2>
  <p style="color:#59636e;font-size:.82rem;margin-bottom:8px">
    Drag to pan, scroll or use the +/- controls to zoom. Circles: size and colour = mean
    wind speed, arrow = direction the wind is blowing toward. Shaded grid overlay: wind
    convergence, interpolated from station wind vectors (u/v averaged onto a grid, then
    the grid's divergence computed) — each tile is one grid cell.
  </p>
  {conv_scale}
  <div class="map-wrap"><div id="leaflet-map"></div></div>
</section>

<section id="timeseries">
  <h2>Speed Time Series — By Region</h2>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_spag}" alt="time series"></div>
</section>

<section id="ranking">
  <h2>Station Rankings</h2>
  <p style="color:#59636e;font-size:.82rem;margin-bottom:12px">
    Bars show mean (solid) and max (translucent). Blue arrow = direction the wind is blowing toward.
  </p>
  <div class="chart-wrap"><img src="data:image/png;base64,{img_rank}" alt="rankings"></div>
</section>

<section id="table">
  <h2>Summary Table</h2>
  <div class="table-scroll">
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
  Report period: {period_label} · Built {built}<br>
  API-reported units — speed: {speed_unit}, direction: {direction_unit}
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

  L.tileLayer('https://{{s}}.basemaps.cartocdn.com/light_all/{{z}}/{{x}}/{{y}}{{r}}.png', {{
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
    subdomains: 'abcd',
    maxZoom: 19
  }}).addTo(map);

  var convImage = {conv_image};
  if (convImage) {{
    L.imageOverlay(convImage, {conv_bounds}, {{opacity: 1, interactive: false}}).addTo(map);
  }}

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
      color: '#1f2328',
      weight: 1,
      fillOpacity: 0.85
    }}).addTo(map).bindPopup(
      '<b>' + s.name + '</b> (' + s.sid + ')<br>' +
      'Mean: ' + s.mean_spd.toFixed(1) + ' km/h' +
      (s.mean_dir !== null ? '<br>Dir: ' + Math.round(s.mean_dir) + '&deg;' : '')
    );

    if (s.mean_dir !== null) {{
      // s.mean_dir is the met-bearing the wind blows FROM; the arrow should
      // point the way the wind is blowing TOWARD, i.e. that bearing + 180.
      var rot = s.mean_dir + 90;
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
    print(f"  Period: {PERIOD_LABEL}")
    print(f"  API key: {'YES' if API_KEY else 'NO (unauthenticated)'}")
    print(f"{'═'*60}\n")

    timestamps, speed_ts, dir_ts, station_info, units = collect()
    n_stations = len(speed_ts)
    n_minutes  = len(timestamps)
    print(f"\n  Collected {n_stations} stations × {n_minutes} minutes\n")

    print("  Building charts …")
    img_spag = chart_spaghetti(timestamps, speed_ts, station_info)
    img_rank = chart_ranking(speed_ts, dir_ts, station_info)
    map_stations = build_map_data(speed_ts, dir_ts, station_info)
    map_data = json.dumps(map_stations).replace("</", "<\\/")
    tbl_rows = build_table(speed_ts, dir_ts, station_info)

    print("  Interpolating convergence field …")
    convergence = build_convergence_field(speed_ts, dir_ts, station_info)
    if convergence is not None:
        conv_img_b64, conv_vmax = render_convergence_overlay(convergence)
        conv_image  = json.dumps("data:image/png;base64," + conv_img_b64)
        conv_bounds = json.dumps(CONV_BOUNDS)
        conv_scale = f"""<div class="conv-scale">
    <div class="conv-gradient"></div>
    <div class="conv-scale-labels">
      <span>&minus;{conv_vmax:.1f} divergence</span>
      <span>0</span>
      <span>+{conv_vmax:.1f} convergence</span>
    </div>
    <div class="conv-scale-caption">Wind convergence, &times;10&#8315;&#8308; s&#8315;&#185; (interpolated)</div>
  </div>"""
    else:
        conv_image  = "null"
        conv_bounds = "null"
        conv_scale  = ""

    html = HTML.format(
        period_label   = PERIOD_LABEL,
        n_stations     = n_stations,
        n_minutes      = n_minutes,
        map_data       = map_data,
        conv_image     = conv_image,
        conv_bounds    = conv_bounds,
        conv_scale     = conv_scale,
        img_spag       = img_spag,
        img_rank       = img_rank,
        table_rows     = tbl_rows,
        speed_unit     = units["speed"] or "not reported by API",
        direction_unit = units["direction"] or "not reported by API",
        built          = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
    )

    out = DOCS / "index.html"
    out.write_text(html, encoding="utf-8")
    print(f"\n  ✓ Site written to {out}  ({len(html)//1024} KB)\n")


if __name__ == "__main__":
    main()
