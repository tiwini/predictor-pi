#!/usr/bin/env python3
"""Muestreo rápido de Kalshi dentro de la ventana de pico.

POR QUÉ EXISTE
--------------
`analysis_poller` escribe `kalshi_snapshots` cada 600 s, y la cadencia real
medida es **11.6 min**. Eso puso techo a las tres mediciones de latencia del
2026-08-20 ([[latencia_vs_kalshi_cerrada_2026_08_20]]): ninguna ventaja menor de
~12 min es observable, así que un negativo significaba "no detectable a esta
resolución", nunca "no existe".

Aquellas tres salieron negativas con margen amplio (el mercado mata bins 99 min
antes), así que **esto no es para reabrir esa pregunta** — es para que las
siguientes se puedan contestar. El dato que no se guarda con resolución
suficiente no se puede analizar después, y ya ha pasado tres veces con
`current_temp_stable_min`, `current_obs_ts` y `today_max_asos_6h`.

DECISIONES DE DISEÑO
--------------------
**Tabla propia, no `kalshi_snapshots`.** Meter filas más densas en la serie
existente le cambiaría la densidad a mitad de camino, y esa serie es la base de
los backtests. Es el mismo criterio que separó `today_max_cli` de
`today_max_obs`: no se le cambia la semántica a una serie viva.

**Se guardan bid, ask y mid.** El `yes_mid` de un libro fino se queda pegado
aunque todo el mundo sepa que el bin está muerto; sin bid/ask no se puede
distinguir "el mercado no reaccionó" de "no había nadie al otro lado". Esa
hipótesis quedó sin poder comprobarse en el análisis del 08-20.

**Sólo estaciones dentro de su ventana de pico**, que es cuando el precio se
mueve y cuando la resolución importa. Fuera de ventana el poller de 10 min ya
cubre. Usa `PEAK_HOURS`, recalibrada el 2026-08-18.

**Proceso aparte**, como `btc_quarter_poller`. Si falla, no arrastra al
colector principal — que es el que alimenta los backtests.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import kalshi
from stations import STATION_IDS, PEAK_HOURS, STATION_TZ
from zoneinfo import ZoneInfo

INTERVAL_S = 120          # 2 min: para ver ventanas de ~5 min hace falta esto
DB_PATH = Path(__file__).parent / "analysis.db"
# Margen antes de que abra la ventana y después de que cierre: el pico real
# puede desplazarse y lo interesante ocurre en los bordes.
MARGEN_H = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [kalshi_fast] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kalshi_fast")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=30000")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS kalshi_fast (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            station TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            yes_bid REAL,
            yes_ask REAL,
            yes_mid REAL
        );
        CREATE INDEX IF NOT EXISTS idx_kf_station_ts
            ON kalshi_fast(station, ts);
        CREATE INDEX IF NOT EXISTS idx_kf_ticker_ts
            ON kalshi_fast(ticker, ts);
    """)
    return c


def en_ventana(sid: str) -> bool:
    """¿Está la estación dentro de su ventana de pico, con margen?"""
    lo, hi = PEAK_HOURS.get(sid, (12, 17))
    h = datetime.now(ZoneInfo(STATION_TZ[sid])).hour
    return (lo - MARGEN_H) <= h < (hi + MARGEN_H)


def ciclo(c: sqlite3.Connection) -> tuple[int, int]:
    activas = [s for s in STATION_IDS if en_ventana(s)]
    ts = datetime.now(timezone.utc).isoformat()
    filas = 0
    # Las 20 estaciones pueden estar en ventana a la vez (medido: hasta 20
    # simultáneas, 600 peticiones/hora). Se espacian dentro del ciclo en vez de
    # soltarlas de golpe: 0.3 s × 20 = 6 s sobre un intervalo de 120, o sea
    # ruido, y evita una ráfaga de 20 peticiones en el mismo segundo.
    for i, sid in enumerate(activas):
        if i:
            time.sleep(0.3)
        try:
            hoy = datetime.now(ZoneInfo(STATION_TZ[sid])).date()
            bins = kalshi.fetch_bins(sid, hoy)
        except Exception as e:
            log.warning("%s: %s", sid, str(e)[:80])
            continue
        for b in bins:
            c.execute(
                """INSERT INTO kalshi_fast
                   (ts, station, ticker, bin_lo, bin_hi, yes_bid, yes_ask, yes_mid)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts, sid, b.ticker, b.bin_lo, b.bin_hi,
                 b.yes_bid, b.yes_ask, b.yes_mid))
            filas += 1
    c.commit()
    return len(activas), filas


def main() -> int:
    log.info("arrancado · cada %ds · sólo estaciones en ventana ±%dh",
             INTERVAL_S, MARGEN_H)
    c = _conn()
    while True:
        t0 = time.time()
        try:
            n_est, n_filas = ciclo(c)
            if n_est:
                log.info("%d estaciones en ventana · %d filas", n_est, n_filas)
        except Exception as e:
            log.error("ciclo falló: %s", str(e)[:120])
        # Cadencia estable: descuenta lo que tardó el ciclo.
        time.sleep(max(5.0, INTERVAL_S - (time.time() - t0)))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        log.info("parado por el usuario")
        sys.exit(0)
