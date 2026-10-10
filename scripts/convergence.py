"""
convergence.py — hourly wind-convergence fields over Singapore, computed from
the stored 5-minute station readings.

Method
  * Station wind -> eastward/northward components (u, v). Never interpolate
    speed or direction directly.
  * Locally weighted linear regression on a 1.5 km grid: around each cell, fit a
    plane u = a + b.x + c.y (and likewise v) to the stations with Gaussian
    weights (length scale ~ station spacing). The fitted slopes are the wind
    gradients, so divergence = du/dx + dv/dy comes straight out. Unlike a
    weighted-average (Barnes / inverse-distance) analysis this is exact for a
    linear wind field and is not biased by uneven station spacing.
  * Convergence C = -(du/dx + dv/dy), in units of 1e-5 s^-1 (positive = air
    piling up, the signature of sea-breeze fronts, outflow boundaries, etc.).
  * Station exposure differs (a rooftop gauge vs a sheltered one can differ by
    a factor of 3 in mean speed), which would show up as a permanent fake
    convergence pattern. So every layer is computed from each station's wind
    ANOMALY: the reading minus that station's own mean over the stored 24 h.
    The constant pattern this removes is stored once as "base" so the page can
    add it back for the hourly-mean layer.
  * Layers per clock hour (SGT), all of anomaly wind:
      mean  convergence of the hourly vector-mean wind
      peak  highest 15-min-smoothed 5-min convergence in the hour
      pos   hour-average of the convergent part only (for "persistence")
  * Cells farther than MASK_KM from every station are left blank.

All fields are quantised to int8 (0.5 x 1e-5 s^-1 steps) and base64-encoded.
"""

import base64, math, warnings
from datetime import datetime, timedelta

import numpy as np

# ── Parameters ────────────────────────────────────────────────────────────────
LAT0, LON0 = 1.3521, 103.8198          # projection origin
KX = 111.320 * math.cos(math.radians(LAT0))   # km per degree longitude
KY = 110.574                                  # km per degree latitude
CELL_KM = 1.5
LON_RANGE = (103.58, 104.12)
LAT_RANGE = (1.14, 1.50)

L1_KM = 5.0          # Gaussian length scale, w = exp(-r^2 / 2L^2)
RIDGE = 0.05         # slope shrinkage; keeps edge cells with few stations stable
MASK_KM = 6.0        # blank beyond this distance from the nearest station
SOLID_KM = 4.0       # full opacity within this distance; faded between SOLID and MASK
MIN_STATIONS = 8     # a field needs at least this many reporting stations
SMOOTH_STEPS = 3     # centred running mean (15 min at 5-min steps) for 5-min fields
HOURS_KEPT = 24
MIN_STEPS_IN_HOUR = 3
MIN_COVERAGE = 0.5   # a station needs readings for half the stored period to be used

SCALE = 1            # int8 value = round(C * SCALE); C in 1e-5 s^-1
NODATA = -128
CLIP = 127

TS_FMT = "%Y-%m-%dT%H:%M"


def _xy(lon, lat):
    return (np.asarray(lon) - LON0) * KX, (np.asarray(lat) - LAT0) * KY


class Grid:
    def __init__(self):
        x_lo, y_lo = _xy(LON_RANGE[0], LAT_RANGE[0])
        x_hi, y_hi = _xy(LON_RANGE[1], LAT_RANGE[1])
        self.nx = int(math.ceil((x_hi - x_lo) / CELL_KM))
        self.ny = int(math.ceil((y_hi - y_lo) / CELL_KM))
        self.x = x_lo + (np.arange(self.nx) + 0.5) * CELL_KM
        self.y = y_lo + (np.arange(self.ny) + 0.5) * CELL_KM
        self.X, self.Y = np.meshgrid(self.x, self.y)          # (ny, nx)
        self.lon0 = LON0 + self.x[0] / KX
        self.lat0 = LAT0 + self.y[0] / KY

    def meta(self):
        return {"lon0": round(self.lon0, 5), "lat0": round(self.lat0, 5),
                "dlon": round(CELL_KM / KX, 6), "dlat": round(CELL_KM / KY, 6),
                "nx": self.nx, "ny": self.ny, "cell_km": CELL_KM}


class Analysis:
    """Local-linear wind gradients for a fixed station network (any subset may report)."""

    def __init__(self, grid, sx, sy):
        self.g = grid
        gx, gy = grid.X.ravel(), grid.Y.ravel()
        self.DX = np.asarray(sx)[None, :] - gx[:, None]        # station minus cell, km
        self.DY = np.asarray(sy)[None, :] - gy[:, None]
        d2 = self.DX ** 2 + self.DY ** 2
        self.W = np.exp(-d2 / (2 * L1_KM ** 2))
        self.dmin = np.sqrt(d2.min(axis=1)).reshape(grid.ny, grid.nx)
        self._cache = {}

    def _moments(self, valid):
        key = valid.tobytes()
        if key not in self._cache:
            W, DX, DY = self.W[:, valid], self.DX[:, valid], self.DY[:, valid]
            S0 = W.sum(1)
            Sx, Sy = (W * DX).sum(1), (W * DY).sum(1)
            Sxx, Sxy, Syy = (W * DX * DX).sum(1), (W * DX * DY).sum(1), (W * DY * DY).sum(1)
            lam = RIDGE * S0 * L1_KM ** 2
            M = np.zeros((len(S0), 3, 3))
            M[:, 0, 0] = S0
            M[:, 0, 1] = M[:, 1, 0] = Sx
            M[:, 0, 2] = M[:, 2, 0] = Sy
            M[:, 1, 1] = Sxx + lam
            M[:, 1, 2] = M[:, 2, 1] = Sxy
            M[:, 2, 2] = Syy + lam
            self._cache[key] = (W, DX, DY, M, {})
        return self._cache[key]

    def convergence(self, valid, u_kmh, v_kmh):
        """Convergence field (1e-5 s^-1) from station u, v in km/h."""
        W, DX, DY, M, _ = self._moments(valid)
        f = np.stack([u_kmh[valid], v_kmh[valid]], axis=1) / 3.6      # (n, 2) m/s
        rhs = np.stack([W @ f, (W * DX) @ f, (W * DY) @ f], axis=1)   # (cells, 3, 2)
        beta = np.linalg.solve(M, rhs)                                # (cells, 3, 2)
        dudx = beta[:, 1, 0] / 1000.0                                  # (m/s per km) -> s^-1
        dvdy = beta[:, 2, 1] / 1000.0
        return (-(dudx + dvdy) * 1e5).reshape(self.g.ny, self.g.nx)

    def _coef(self, valid):
        """Linear weights (cells, n_valid) turning station values into du/dx and dv/dy."""
        W, DX, DY, M, memo = self._moments(valid)
        if "coef" not in memo:
            A = np.stack([W, W * DX, W * DY], axis=1)                  # (cells, 3, n)
            Z = np.linalg.solve(M, A)
            memo["coef"] = (Z[:, 1, :], Z[:, 2, :])
        return memo["coef"]

    def convergence_sigma(self, valid, var_u, var_v):
        """1-sigma uncertainty of the convergence (1e-5 s^-1) given the variance
        (km/h)^2 of each reporting station's u and v value."""
        gx, gy = self._coef(valid)
        var = (gx ** 2) @ var_u[valid] + (gy ** 2) @ var_v[valid]      # (km/h / km)^2
        return (np.sqrt(var) / 3.6 / 1000.0 * 1e5).reshape(self.g.ny, self.g.nx)


def _quantise(c, mask):
    q = np.clip(np.rint(c * SCALE), -CLIP, CLIP).astype(np.int8)
    q[~mask | ~np.isfinite(c)] = NODATA
    return q


def _b64(a):
    return base64.b64encode(np.ascontiguousarray(a, dtype=np.int8).tobytes()).decode("ascii")


# ── Station time series ───────────────────────────────────────────────────────

def _station_arrays(store, sids, keys):
    n, m = len(sids), len(keys)
    U, V = np.full((n, m), np.nan), np.full((n, m), np.nan)
    for j, k in enumerate(keys):
        r = store["readings"][k]
        s, d = r.get("s", {}), r.get("d", {})
        for i, sid in enumerate(sids):
            if sid in s and sid in d:
                rad = math.radians(d[sid])
                U[i, j] = -s[sid] * math.sin(rad)
                V[i, j] = -s[sid] * math.cos(rad)
    return U, V


def _running_mean(A, w):
    """Centred nan-aware running mean along axis 1; needs >= 2 valid values."""
    n, m = A.shape
    half = w // 2
    out = np.full_like(A, np.nan)
    for j in range(m):
        seg = A[:, max(0, j - half): j + half + 1]
        cnt = np.sum(np.isfinite(seg), axis=1)
        with np.errstate(invalid="ignore"):
            mean = np.nanmean(np.where(np.isfinite(seg), seg, np.nan), axis=1)
        out[:, j] = np.where(cnt >= 2, mean, np.nan)
    return out


# ── Main entry point ──────────────────────────────────────────────────────────

def compute(store, coast=None):
    """Return the JSON-serialisable convergence product, or None if no data."""
    warnings.simplefilter("ignore", RuntimeWarning)   # all-NaN slices are expected (gaps)
    sids = sorted(store["stations"])
    info = store["stations"]
    keys = sorted(k for k, v in store["readings"].items() if v.get("s") or v.get("d"))
    if len(sids) < MIN_STATIONS or not keys:
        return None

    grid = Grid()
    sx, sy = _xy([info[s]["lon"] for s in sids], [info[s]["lat"] for s in sids])
    an = Analysis(grid, sx, sy)
    mask = an.dmin <= MASK_KM
    conf = np.where(an.dmin <= SOLID_KM, 2, np.where(mask, 1, 0)).astype(np.int8)

    when = [datetime.strptime(k, TS_FMT) for k in keys]
    U, V = _station_arrays(store, sids, keys)
    poor = np.isfinite(U).mean(axis=1) < MIN_COVERAGE        # too gappy to trust a mean
    U[poor], V[poor] = np.nan, np.nan
    with np.errstate(invalid="ignore"):
        mu, mv = np.nanmean(U, axis=1), np.nanmean(V, axis=1)
    Ua, Va = U - mu[:, None], V - mv[:, None]
    Us, Vs = _running_mean(Ua, SMOOTH_STEPS), _running_mean(Va, SMOOTH_STEPS)
    have_mean = np.isfinite(mu)
    base_c = (an.convergence(have_mean, np.nan_to_num(mu), np.nan_to_num(mv))
              if have_mean.sum() >= MIN_STATIONS else np.full((grid.ny, grid.nx), np.nan))

    # 5-minute (15-min smoothed) convergence for the peak / convergent-only layers.
    c5 = np.full((len(keys), grid.ny, grid.nx), np.nan, dtype=np.float32)
    for j in range(len(keys)):
        valid = np.isfinite(Us[:, j]) & np.isfinite(Vs[:, j])
        if valid.sum() >= MIN_STATIONS:
            c5[j] = an.convergence(valid, Us[:, j], Vs[:, j])

    bins = {}
    for j, t in enumerate(when):
        bins.setdefault(t.replace(minute=0), []).append(j)

    hours, wind = [], {s: [] for s in sids}
    mean_l, peak_l, pos_l, sig_l = [], [], [], []
    for h in sorted(bins):
        idx = bins[h]
        if len(idx) < MIN_STEPS_IN_HOUR:
            continue
        need = max(2, math.ceil(len(idx) * 2 / 3))
        Uh, Vh = Ua[:, idx], Va[:, idx]
        ok = np.sum(np.isfinite(Uh), axis=1) >= need
        if ok.sum() < MIN_STATIONS:
            continue
        with np.errstate(invalid="ignore"):
            ua_m, va_m = np.nanmean(Uh, axis=1), np.nanmean(Vh, axis=1)
            um, vm = np.nanmean(U[:, idx], axis=1), np.nanmean(V[:, idx], axis=1)   # real wind, for arrows
        mean_c = an.convergence(ok, np.where(ok, ua_m, 0), np.where(ok, va_m, 0))
        with np.errstate(invalid="ignore"):
            nvalid = np.maximum(np.sum(np.isfinite(Uh), axis=1), 1)
            var_um = np.where(ok, np.nanvar(Uh, axis=1, ddof=1) / nvalid, 0.0)
            var_vm = np.where(ok, np.nanvar(Vh, axis=1, ddof=1) / nvalid, 0.0)
        sig_c = an.convergence_sigma(ok, np.nan_to_num(var_um), np.nan_to_num(var_vm))

        block = c5[idx]
        have = np.isfinite(block).any(axis=(1, 2))
        if have.any():
            with np.errstate(invalid="ignore"):
                peak_c = np.nanmax(block[have], axis=0)
                pos_c = np.nanmean(np.maximum(block[have], 0), axis=0)
        else:
            peak_c = pos_c = np.full((grid.ny, grid.nx), np.nan)

        hours.append({"t": h.strftime(TS_FMT), "n": len(idx), "stn": int(ok.sum())})
        mean_l.append(_quantise(mean_c, mask)); peak_l.append(_quantise(peak_c, mask))
        sig_l.append(_quantise(sig_c * 4, mask))   # stored at 4x scale (0.25 steps)
        pos_l.append(_quantise(pos_c, mask))
        for i, s in enumerate(sids):
            wind[s].append([round(float(um[i]), 1), round(float(vm[i]), 1)] if ok[i] else None)

    if not hours:
        return None
    hours, mean_l, peak_l, pos_l, sig_l = (hours[-HOURS_KEPT:], mean_l[-HOURS_KEPT:], peak_l[-HOURS_KEPT:],
                                           pos_l[-HOURS_KEPT:], sig_l[-HOURS_KEPT:])
    wind = {s: w[-HOURS_KEPT:] for s, w in wind.items()}

    return {
        "v": 1, "unit": "1e-5 s^-1", "scale": SCALE, "nodata": NODATA,
        "grid": grid.meta(),
        "params": {"anomaly": True, "L_km": L1_KM, "ridge": RIDGE, "mask_km": MASK_KM, "solid_km": SOLID_KM,
                   "smooth_min": SMOOTH_STEPS * 5},
        "stations": [{"id": s, "name": info[s]["name"], "lat": info[s]["lat"], "lon": info[s]["lon"],
                      "region": info[s].get("region", "")} for s in sids],
        "hours": hours, "wind": wind,
        "mean": _b64(np.stack(mean_l)), "peak": _b64(np.stack(peak_l)), "pos": _b64(np.stack(pos_l)), "sig": _b64(np.stack(sig_l)),
        "conf": _b64(conf), "base": _b64(_quantise(base_c, mask)),
        "coast": (coast or {}).get("rings", []),
    }
