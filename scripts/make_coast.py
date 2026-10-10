"""
make_coast.py — one-off helper that builds data/sg_coast.json (the outline drawn
on the convergence map) from real boundary data.

Source: geoBoundaries gbOpen, Singapore ADM0 (CC BY 4.0), https://www.geoboundaries.org
Simplified with Ramer–Douglas–Peucker; tiny islets are dropped.

Usage: python scripts/make_coast.py [simplify_tolerance_degrees]
"""

import json, sys, urllib.request
from pathlib import Path

URL = ("https://media.githubusercontent.com/media/wmgeolab/geoBoundaries/main/"
       "releaseData/gbOpen/SGP/ADM0/geoBoundaries-SGP-ADM0.geojson")
OUT = Path(__file__).resolve().parent.parent / "data" / "sg_coast.json"
TOL = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0004   # ~45 m
MIN_AREA_KM2 = 0.4


def rdp(pts, tol):
    """Iterative Ramer–Douglas–Peucker on a list of (x, y)."""
    if len(pts) < 3:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        (x1, y1), (x2, y2) = pts[a], pts[b]
        dx, dy = x2 - x1, y2 - y1
        norm = (dx * dx + dy * dy) ** 0.5
        best, idx = -1.0, -1
        for i in range(a + 1, b):
            x, y = pts[i]
            d = (abs(dy * (x - x1) - dx * (y - y1)) / norm) if norm else ((x - x1) ** 2 + (y - y1) ** 2) ** 0.5
            if d > best:
                best, idx = d, i
        if best > tol:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(pts, keep) if k]


def area_km2(ring):
    s = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        s += x1 * y2 - x2 * y1
    return abs(s) / 2 * 111.32 * 110.57 * 0.9997


def main():
    with urllib.request.urlopen(URL, timeout=120) as r:
        gj = json.load(r)
    geom = gj["features"][0]["geometry"]
    polys = geom["coordinates"] if geom["type"] == "MultiPolygon" else [geom["coordinates"]]
    rings = []
    for poly in polys:
        outer = [tuple(p[:2]) for p in poly[0]]
        if area_km2(outer) < MIN_AREA_KM2:
            continue
        simp = rdp(outer, TOL)
        if len(simp) >= 4:
            rings.append([[round(x, 4), round(y, 4)] for x, y in simp])
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps({
        "source": "geoBoundaries gbOpen SGP ADM0 (CC BY 4.0), simplified",
        "rings": rings}, separators=(",", ":")))
    n = sum(len(r) for r in rings)
    print(f"{len(rings)} rings, {n} points, {OUT.stat().st_size/1024:.1f} KB -> {OUT}")


if __name__ == "__main__":
    main()
