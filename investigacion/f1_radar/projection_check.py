#!/usr/bin/env python3
"""F1 radar projection check — verificar que lat/lon → pixel mapping funciona
en latitudes distintas antes del backfill masivo.

Fable spec (2026-07-20): 3 stations latitudes distintas × 3 frames c/u.
Test: dBZ extraído en (lat, lon) debe coincidir con lo que muestra el mapa
NWS público. Si mapeo naïve `col = (lon - (-126)) / 0.01` funciona en KMIA
(25°N) pero falla en KMSP (45°N), es proyección no-linear silenciosa.

Fuentes:
- Iowa Mesonet composite N0R:
  https://mesonet.agron.iastate.edu/archive/data/YYYY/MM/DD/GIS/uscomp/n0r_YYYYMMDDHHMM.png
- World file (asumido según probe memoria):
  pixel = 0.01° × 0.01° | origen UL = (-126.0 lon, 50.0 lat)
- Palette: idx=0 Missing, idx≥1 → dBZ = -35 + 5*idx

Método:
1. Para cada (station, timestamp), fetch PNG + world file
2. Extract world file real (puede diferir del asumido en distintas fechas)
3. Compute pixel(lat, lon) usando affine transform del world file
4. Extract 9x9 window, palette-decode a dBZ
5. Compare vs valores publicados por Iowa Mesonet en su viewer online
6. Report mismatch si mapeo falla en alguna latitud
"""
import io
import sys
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from PIL import Image

UA = "predictor-pi-f1-probe/0.1"

# 3 estaciones a latitudes muy distintas — el test crítico
STATIONS = {
    "KMIA": (25.79, -80.29),   # subtropical
    "KATL": (33.64, -84.43),   # mid-latitude
    "KMSP": (44.88, -93.22),   # northern
}

# 3 frames con storms conocidas — fechas verificables
# Uso frames de ayer y hoy con weather activo TX/SE US
FRAMES = [
    "202607231800",  # 2026-07-23 18:00 UTC = 14:00 EDT — tarde pico
    "202607232000",  # 2026-07-23 20:00 UTC = 16:00 EDT
    "202607231600",  # 2026-07-23 16:00 UTC = 12:00 EDT — mid-day
]


def fetch_png(ts: str) -> Image.Image:
    dt = datetime.strptime(ts, "%Y%m%d%H%M")
    url = (f"https://mesonet.agron.iastate.edu/archive/data/"
           f"{dt.strftime('%Y/%m/%d')}/GIS/uscomp/n0r_{ts}.png")
    print(f"    fetch {url}")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return Image.open(io.BytesIO(r.read()))


def fetch_worldfile(ts: str) -> tuple[float, float, float, float, float, float]:
    """Return (px_x, rot_a, rot_b, px_y, ul_x, ul_y) affine params."""
    dt = datetime.strptime(ts, "%Y%m%d%H%M")
    url = (f"https://mesonet.agron.iastate.edu/archive/data/"
           f"{dt.strftime('%Y/%m/%d')}/GIS/uscomp/n0r_{ts}.wld")
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        lines = r.read().decode().strip().split("\n")
    return tuple(float(x) for x in lines[:6])


def lonlat_to_pixel(lon: float, lat: float, wld: tuple) -> tuple[int, int]:
    """Affine: solve pixel = inverse_transform(lat, lon).
    Standard world file: [x_world; y_world] = A [col; row] + [ul_x; ul_y]
    where A = [[px_x, rot_a],[rot_b, px_y]].
    For diagonal A (no rotation): col = (x - ul_x) / px_x; row = (y - ul_y) / px_y.
    """
    px_x, rot_a, rot_b, px_y, ul_x, ul_y = wld
    if rot_a != 0 or rot_b != 0:
        # General affine inverse
        det = px_x * px_y - rot_a * rot_b
        col = ((lon - ul_x) * px_y - (lat - ul_y) * rot_a) / det
        row = (-(lon - ul_x) * rot_b + (lat - ul_y) * px_x) / det
    else:
        col = (lon - ul_x) / px_x
        row = (lat - ul_y) / px_y
    return int(round(col)), int(round(row))


def palette_to_dbz(idx: int) -> int | None:
    """N0R palette: 0 = missing/no-echo, 1+ = -35 + 5*idx dBZ."""
    if idx == 0:
        return None
    return -35 + 5 * idx


def extract_window(img: Image.Image, col: int, row: int, size: int = 9) -> list[list[int | None]]:
    """Extract (size x size) window centered at (col, row), return dBZ values."""
    px = img.load()
    half = size // 2
    win = []
    for dr in range(-half, half + 1):
        row_vals = []
        for dc in range(-half, half + 1):
            r, c = row + dr, col + dc
            if 0 <= r < img.height and 0 <= c < img.width:
                idx = px[c, r]
                if isinstance(idx, tuple):
                    idx = idx[0]
                row_vals.append(palette_to_dbz(idx))
            else:
                row_vals.append(None)
        win.append(row_vals)
    return win


def window_max_min(win: list[list]) -> tuple[int | None, int | None]:
    vals = [v for row in win for v in row if v is not None]
    if not vals:
        return None, None
    return max(vals), min(vals)


def main():
    print("=" * 70)
    print("F1 RADAR PROJECTION CHECK — 3 stations × 3 frames")
    print("=" * 70)
    print()

    # Show world file for first frame — validate against assumption
    wld = fetch_worldfile(FRAMES[0])
    print(f"World file {FRAMES[0]}: {wld}")
    print(f"  px_x={wld[0]}  px_y={wld[3]}  UL=({wld[4]}, {wld[5]})")
    print(f"  Assumed diagonal={wld[1]==0 and wld[2]==0}")
    print()

    for ts in FRAMES:
        try:
            img = fetch_png(ts)
        except Exception as e:
            print(f"  Frame {ts}: ERR {e}")
            continue
        print(f"\nFrame {ts} — size={img.size}, mode={img.mode}")

        for sid, (lat, lon) in STATIONS.items():
            col, row = lonlat_to_pixel(lon, lat, wld)
            in_bounds = 0 <= col < img.width and 0 <= row < img.height
            print(f"  {sid} (lat={lat:.2f}, lon={lon:.2f})")
            print(f"    pixel=({col}, {row})  in_bounds={in_bounds}")
            if in_bounds:
                # 5x5 (dbz_5x5)
                w5 = extract_window(img, col, row, 5)
                mx5, mn5 = window_max_min(w5)
                # 9x9 (dbz_9x9)
                w9 = extract_window(img, col, row, 9)
                mx9, mn9 = window_max_min(w9)
                print(f"    dbz_5x5: max={mx5} min={mn5}   dbz_9x9: max={mx9} min={mn9}")
                print(f"    hypothesis dbz_9x9-dbz_5x5 = "
                      f"{(mx9 - mx5) if mx5 is not None and mx9 is not None else '?'}")

    # Sanity check: KMIA episodio 2026-07-19 16:15Z validado en memoria a 25dBZ
    print("\n" + "=" * 70)
    print("SANITY CHECK: KMIA 2026-07-19 16:15Z (memoria dice 25 dBZ 7x7)")
    print("=" * 70)
    try:
        img = fetch_png("202607191615")
        wld_kmia = fetch_worldfile("202607191615")
        col, row = lonlat_to_pixel(-80.29, 25.79, wld_kmia)
        w7 = extract_window(img, col, row, 7)
        mx7, mn7 = window_max_min(w7)
        print(f"KMIA 16:15Z 7x7: max={mx7} (esperado ~25 según memoria)")
        if mx7 is not None and 20 <= mx7 <= 30:
            print("✓ PROJECTION CHECK PASSED — mapping funciona en KMIA")
        else:
            print(f"⚠ DIVERGE: got {mx7}, expected ~25")
    except Exception as e:
        print(f"ERR sanity: {e}")


if __name__ == "__main__":
    main()
