#!/usr/bin/env python3
"""Periodos de régimen excepcional que NO deben contaminar backtests.

Por qué existe
--------------
La calibración del predictor (z + PIT con df=5) se ajustó sobre distribuciones
de régimen normal. Un shock de confianza estructural genera colas que ese df no
contempla, así que las probabilidades salen mal calibradas mientras dura — y si
esos días entran en un backtest, se arrastra el problema hacia adelante.

Mismo criterio que en weather con el ledger roto (2026-07-07): el periodo malo
se marca explícitamente y los análisis lo excluyen o lo tratan aparte, en vez de
descubrirlo meses después.

NO modifica ninguna tabla existente. Sólo añade `regime_events` y la consulta.

Uso:
    python3 regime_events.py                 # lista los periodos y su impacto
    python3 regime_events.py --check         # cuántas filas caen dentro
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone

DB_PATH = "/home/popeye/crypto-predictor/calibration.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS regime_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    start_ts   REAL NOT NULL,          -- unix epoch UTC
    end_ts     REAL,                   -- NULL = en curso
    severity   TEXT,                   -- alta | media | baja
    reason     TEXT,
    created_at TEXT
);
"""

# Periodos conocidos. `end_ts=None` significa que sigue abierto: al cerrarlo,
# actualizar aquí y volver a correr el script.
EVENTOS = [
    {
        "name": "coldcard_seed_exploit_2026_07",
        "start": "2026-07-30T00:00:00+00:00",
        "end": None,
        "severity": "alta",
        "reason": (
            "Fallo de firmware Coldcard (introducido en 4.0.0, marzo 2021) que "
            "hacía las seed phrases computacionalmente enumerables: el "
            "dispositivo se saltaba el generador de aleatoriedad por hardware. "
            "El 2026-07-30 se drenaron ~1000 BTC (~$70M) de ~1196 carteras en "
            "41 min, muchas inmóviles durante años. No es un hackeo de "
            "exchange ni un descuido de usuario: rompe el supuesto del "
            "self-custody offline, así que el shock de confianza es "
            "estructural y las colas de la distribución no se parecen a las "
            "del régimen sobre el que se ajustó df=5."
        ),
    },
]


def _iso_to_ts(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="sólo contar filas afectadas, sin escribir")
    args = ap.parse_args()

    con = sqlite3.connect(DB_PATH)
    if not args.check:
        con.executescript(SCHEMA)
        for e in EVENTOS:
            con.execute(
                """INSERT INTO regime_events
                   (name, start_ts, end_ts, severity, reason, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                     start_ts=excluded.start_ts, end_ts=excluded.end_ts,
                     severity=excluded.severity, reason=excluded.reason""",
                (e["name"], _iso_to_ts(e["start"]),
                 _iso_to_ts(e["end"]) if e["end"] else None,
                 e["severity"], e["reason"],
                 datetime.now(timezone.utc).isoformat()))
        con.commit()

    print(f"{'evento':34s} {'desde':>17s} {'hasta':>10s} {'sev':>6s}")
    try:
        rows = con.execute(
            "SELECT name, start_ts, end_ts, severity FROM regime_events "
            "ORDER BY start_ts").fetchall()
    except sqlite3.OperationalError:
        print("  (la tabla no existe todavía: corre sin --check)")
        return 1
    for name, s, e, sev in rows:
        ini = datetime.fromtimestamp(s, timezone.utc).strftime("%Y-%m-%d %H:%M")
        fin = (datetime.fromtimestamp(e, timezone.utc).strftime("%Y-%m-%d")
               if e else "EN CURSO")
        print(f"{name:34s} {ini:>17s} {fin:>10s} {sev:>6s}")

        for tabla, col in (("hourly_calls", "made_at"),
                           ("predictions", "made_at")):
            try:
                q = f"SELECT COUNT(*) FROM {tabla} WHERE {col} >= ?"
                p = [s]
                if e:
                    q += f" AND {col} < ?"
                    p.append(e)
                n = con.execute(q, p).fetchone()[0]
                tot = con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0]
                print(f"    {tabla:16s} {n:7d} de {tot:8d} filas dentro "
                      f"({100 * n / tot:.1f}%)")
            except sqlite3.OperationalError:
                pass
    print("\nCómo usarlo en un backtest:")
    print("  excluir:  WHERE made_at NOT BETWEEN start_ts AND COALESCE(end_ts, 9e9)")
    print("  o tratar el periodo aparte y reportar los dos resultados.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
