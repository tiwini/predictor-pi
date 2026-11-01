"""¿El mid pegado es opinión del mercado o libro de un solo lado?

Pregunta 3 de las cinco que quedaron escritas al montar el colector rápido el
2026-08-20. Criterios en investigacion/preregistro_mid_libro_vacio_2026_10_31.md,
escrito ANTES de esta consulta.

Sólo lee. Dos denominadores: por fila (autocorrelacionada, 2 min de cadencia) y
por (día, estación, bin) única, que es el que decide.
"""
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "weather-predictor"))

DB = ROOT / "weather-predictor" / "analysis.db"

MID_BAJO = 0.02
MID_ALTO = 0.98
MEDIANA = 0.5  # una tripleta cuenta como de un lado si lo estuvo en la mediana

BASE = "yes_bid IS NOT NULL AND yes_ask IS NOT NULL AND yes_mid IS NOT NULL"


def por_fila(c):
    return c.execute(f"""
        SELECT COUNT(*),
               SUM(yes_mid <= {MID_BAJO}),
               SUM(yes_mid <= {MID_BAJO} AND yes_bid = 0),
               SUM(yes_mid >= {MID_ALTO}),
               SUM(yes_mid >= {MID_ALTO} AND yes_ask >= 1.0)
        FROM kalshi_fast WHERE {BASE}""").fetchone()


def por_tripleta(c, cond_universo, cond_un_lado):
    """Fracción de (día, estación, bin) que estuvo de un solo lado en la
    mediana de sus observaciones dentro del universo."""
    return c.execute(f"""
        WITH t AS (
          SELECT substr(ts,1,10) AS d, station, bin_lo, bin_hi,
                 COUNT(*) AS n,
                 AVG(CASE WHEN {cond_un_lado} THEN 1.0 ELSE 0.0 END) AS frac
          FROM kalshi_fast
          WHERE {BASE} AND {cond_universo}
          GROUP BY d, station, bin_lo, bin_hi
        )
        SELECT COUNT(*), SUM(frac >= {MEDIANA}), AVG(n) FROM t""").fetchone()


def main():
    c = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)

    print(f"=== kalshi_fast · {DB.name} ===")
    tot, mb, mb_sb, ma, ma_sa = por_fila(c)
    print(f"filas con bid y ask: {tot:,}")
    print()
    print("POR FILA (no decide: 2 min de cadencia, ~30 copias del mismo estado/hora)")
    print(f"  mid <= {MID_BAJO}:        {mb:>9,}  ({100*mb/tot:5.1f}% del total)")
    print(f"    de ellas bid = 0:   {mb_sb:>9,}  ({100*mb_sb/mb:5.1f}% de los mid bajos)")
    print(f"  mid >= {MID_ALTO}:        {ma:>9,}  ({100*ma/tot:5.1f}% del total)")
    print(f"    de ellas ask >= 1:  {ma_sa:>9,}  ({100*ma_sa/ma:5.1f}% de los mid altos)" if ma else "    (sin mid altos)")
    print()

    print("POR (día, estación, bin) ÚNICA  ← el denominador que decide")
    n, un_lado, media_obs = por_tripleta(c, f"yes_mid <= {MID_BAJO}", "yes_bid = 0")
    pct = 100 * un_lado / n if n else 0
    print(f"  mid bajo:  {n:>7,} tripletas  ({media_obs:.1f} obs/tripleta)")
    print(f"    sin bid en la mediana: {un_lado:,}  →  {pct:.1f}%")
    n2, un_lado2, media2 = por_tripleta(c, f"yes_mid >= {MID_ALTO}", "yes_ask >= 1.0")
    pct2 = 100 * un_lado2 / n2 if n2 else 0
    print(f"  mid alto:  {n2:>7,} tripletas  ({media2:.1f} obs/tripleta)")
    print(f"    sin ask en la mediana: {un_lado2:,}  →  {pct2:.1f}%")
    print()

    veredicto = ("≥60% → marcar «sin bid» y dejar de reportar mispricing ahí" if pct >= 60
                 else "30-60% → exponer bid/ask, no excluir nada" if pct >= 30
                 else "<30% → sospecha del 08-20 REFUTADA, no se cambia nada")
    print(f"VEREDICTO contra el pre-registro: {pct:.1f}%  →  {veredicto}")
    print()

    print("=== el ask cuando el mid es bajo y no hay bid ===")
    for ask, n in c.execute(f"""SELECT yes_ask, COUNT(*) FROM kalshi_fast
            WHERE {BASE} AND yes_mid <= {MID_BAJO} AND yes_bid = 0
            GROUP BY yes_ask ORDER BY 2 DESC LIMIT 6"""):
        print(f"  ask {ask:<6} {n:>9,}")
    print()

    print("=== por estación (tripletas de mid bajo) ===")
    print(f"  {'est':<6} {'tripletas':>9} {'sin bid':>8} {'%':>6}")
    for st, n, sb in c.execute(f"""
            WITH t AS (
              SELECT substr(ts,1,10) AS d, station, bin_lo, bin_hi,
                     AVG(CASE WHEN yes_bid = 0 THEN 1.0 ELSE 0.0 END) AS frac
              FROM kalshi_fast WHERE {BASE} AND yes_mid <= {MID_BAJO}
              GROUP BY d, station, bin_lo, bin_hi)
            SELECT station, COUNT(*), SUM(frac >= {MEDIANA}) FROM t
            GROUP BY station ORDER BY 4 - 4"""):
        print(f"  {st:<6} {n:>9,} {sb:>8,} {100*sb/n:>5.1f}%")
    print()

    print("=== cobertura: días y rango ===")
    for row in c.execute("""SELECT COUNT(DISTINCT substr(ts,1,10)),
            MIN(substr(ts,1,10)), MAX(substr(ts,1,10)) FROM kalshi_fast"""):
        print(f"  {row[0]} días  ({row[1]} → {row[2]})")

    print()
    print("=== ¿hay filas fuera de la ventana de pico? (hora local) ===")
    try:
        from stations import STATION_TZ
        from zoneinfo import ZoneInfo
        from datetime import datetime
        horas = {}
        for st, ts in c.execute("""SELECT station, ts FROM kalshi_fast
                WHERE id % 97 = 0"""):  # muestra 1/97, basta para el histograma
            tz = STATION_TZ.get(st)
            if not tz:
                continue
            h = datetime.fromisoformat(ts).astimezone(ZoneInfo(tz)).hour
            horas[h] = horas.get(h, 0) + 1
        for h in sorted(horas):
            print(f"  {h:02d}h local  {'#' * max(1, horas[h] * 40 // max(horas.values()))} {horas[h]}")
    except Exception as e:
        print(f"  (no se pudo: {e})")

    c.close()


if __name__ == "__main__":
    main()
