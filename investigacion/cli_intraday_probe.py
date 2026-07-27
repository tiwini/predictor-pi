"""Probe F2 pieza 1 — ¿sirve el CLI parcial para saber el max INTRADÍA?

Pregunta que responde (antes de diseñar nada):
  1. ¿Cuántos CLI emite cada WFO por día y a qué hora LOCAL de la estación?
  2. ¿El parcial de la tarde trae ya el max FINAL del día, o se queda corto?
  3. ¿Cuánto antes del cierre lo sabríamos?

Read-only, sólo api.weather.gov. No toca DB.
"""
from __future__ import annotations

import statistics
import sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

sys.path.insert(0, "/home/popeye/predictor-pi/weather-predictor")
import nws_cli  # noqa: E402
from stations import STATION_TO_LOCATION, STATION_TZ  # noqa: E402

UA = nws_cli.UA
API = nws_cli.API
HEADERS = {"User-Agent": UA, "Accept": "application/ld+json"}


def probe(station: str, limit: int = 14) -> list[dict]:
    loc = STATION_TO_LOCATION[station]
    tz = ZoneInfo(STATION_TZ[station])
    r = requests.get(f"{API}/products",
                     params={"type": "CLI", "location": loc, "limit": limit},
                     headers=HEADERS, timeout=25)
    r.raise_for_status()
    out = []
    for item in r.json().get("@graph", []):
        pid = item.get("id")
        iss = item.get("issuanceTime")
        if not (pid and iss):
            continue
        try:
            r2 = requests.get(f"{API}/products/{pid}", headers=HEADERS,
                              timeout=25)
        except requests.RequestException:
            continue
        if r2.status_code != 200:
            continue
        text = r2.json().get("productText", "")
        iss_dt = datetime.fromisoformat(iss).astimezone(tz)
        out.append({
            "station": station,
            "issued_local": iss_dt,
            "summary_date": nws_cli._parse_summary_date(text),
            "max": nws_cli._parse_max(text),
            "min": nws_cli._parse_min(text),
        })
    return out


def analyze(station: str, rows: list[dict], verbose: bool) -> dict | None:
    by_day: dict[object, list[dict]] = defaultdict(list)
    for row in rows:
        if row["summary_date"]:
            by_day[row["summary_date"]].append(row)

    if verbose:
        print(f"\n=== {station}  ({STATION_TZ[station]}) ===")

    late_hours: list[float] = []
    agree = miss = 0
    deltas: list[float] = []
    for day in sorted(by_day, reverse=True):
        seq = sorted(by_day[day], key=lambda r: r["issued_local"])
        if verbose:
            print(f"  summary {day}:")
            for row in seq:
                il = row["issued_local"]
                tag = "MISMO DÍA" if il.date() == day else "post-cierre"
                print(f"    emitido {il:%m-%d %H:%M} local ({tag:11s}) "
                      f"max={row['max']} min={row['min']}")

        # el "parcial de la tarde" = último emitido el MISMO día
        same_day = [r for r in seq if r["issued_local"].date() == day
                    and r["max"] is not None]
        final = [r for r in seq if r["issued_local"].date() > day
                 and r["max"] is not None]
        if not same_day:
            continue
        late = same_day[-1]
        # sólo cuenta como "de la tarde" si es después de mediodía local
        if late["issued_local"].hour < 12:
            continue
        late_hours.append(late["issued_local"].hour
                          + late["issued_local"].minute / 60)
        if final:
            d = late["max"] - final[-1]["max"]
            deltas.append(d)
            if abs(d) < 0.01:
                agree += 1
            else:
                miss += 1

    if not late_hours:
        return None
    return {
        "station": station,
        "n_late": len(late_hours),
        "hour_med": statistics.median(late_hours),
        "hour_min": min(late_hours),
        "hour_max": max(late_hours),
        "n_pairs": agree + miss,
        "agree": agree,
        "miss": miss,
        "deltas": deltas,
    }


def main(stations: list[str], verbose: bool) -> None:
    summary = []
    for st in stations:
        try:
            rows = probe(st)
        except Exception as exc:  # noqa: BLE001
            print(f"{st}: ERROR {exc}")
            continue
        got = analyze(st, rows, verbose)
        if got:
            summary.append(got)
        else:
            print(f"{st}: sin CLI de tarde en la ventana servida")

    print("\n\n===== RESUMEN: ¿el CLI de la tarde ya trae el max final? =====")
    print(f"{'st':6s} {'hora local':>16s}  {'N':>3s}  "
          f"{'== final':>9s}  {'!=':>3s}  deltas (tarde − final)")
    tot_a = tot_m = 0
    for s in sorted(summary, key=lambda r: r["hour_med"]):
        h = s["hour_med"]
        rng = f"{int(h):02d}:{int(h % 1 * 60):02d}"
        span = f"({s['hour_min']:.1f}-{s['hour_max']:.1f})"
        ds = " ".join(f"{d:+.0f}" for d in s["deltas"]) or "—"
        print(f"{s['station']:6s} {rng:>8s} {span:>8s}  {s['n_late']:3d}  "
              f"{s['agree']:5d}/{s['n_pairs']:<3d}  {s['miss']:3d}  {ds}")
        tot_a += s["agree"]
        tot_m += s["miss"]
    if tot_a + tot_m:
        print(f"\nTOTAL: {tot_a}/{tot_a + tot_m} días en que el CLI de la "
              f"tarde ya iguala el final ({100 * tot_a / (tot_a + tot_m):.0f}%)")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    main(args or list(STATION_TO_LOCATION), "-v" in sys.argv)
