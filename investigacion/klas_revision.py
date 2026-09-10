#!/usr/bin/env python3
"""Revisión de KLAS con N≥40, escrita el 2026-09-10 con N=13.

Por qué existe. KLAS **entró fallando su propio criterio**: la corrida del
2026-08-28 pedía (a) mejora ≥0.75°F, (b) acertar en ≥65% con p<0.05 y (c) que
el acierto de bin no bajara, y salió mejora 0.66 con 17/25 (p=0.054). El
veredicto escrito fue «⏸ ESPERAR, no entra… se revisa con N≥40». Se habilitó
igualmente, restringida a 9-13h, y esa ventana se eligió **barriendo las horas
sobre la misma muestra que acababa de fallar**. Es la ✅ más débil del roster y
la señaló la auditoría externa del 2026-09-07.

Esto la juzga con días que nadie usó para decidir nada: todos los posteriores
al despliegue del 2026-08-28.

CRITERIO — el mismo con el que se la evaluó, sin rebajas:
  (a) |err| publicado ≤ |err| sin corrector − 0.75°F
  (b) acerca en ≥65% de los días, con p<0.05 unilateral (signos)
  (c) el acierto de bin no baja
  (d) la ventana 9-13h se sostiene  ← informativo hasta que haya sombra fuera
      de ventana (se empezó a registrar el 2026-09-10, ver `bias_frozen_f`)

Si (a), (b) y (c) pasan: se queda. Si falla (a) o (b): se retira, porque es
exactamente lo que dijo su corrida de entrada y entonces se ignoró.
"""
import math
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
import sqlite3

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402

ST = "KLAS"
N_OBJETIVO = 40
MEJORA_MIN_F = 0.75
ACIERTA_MIN_FRAC = 0.65
UTC = ZoneInfo("UTC")
HORA = 12
VENTANA_MIN = 45


def p_signos_unilateral(k, n):
    if n <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def bin_de(bins, valor):
    for lo, hi in bins:
        LO = lo if abs(lo) < 1e8 else float("-inf")
        HI = hi if abs(hi) < 1e8 else float("inf")
        if (LO - 0.5) <= valor <= (HI + 0.5):
            return (lo, hi)
    return None


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    tz = ZoneInfo(STATION_TZ[ST])
    desde = an.execute(
        """SELECT MIN(date(datetime(ts,'localtime'))) FROM station_snapshots
           WHERE station=? AND bias_path='median_causal' AND bias_applied=1""",
        (ST,)).fetchone()[0]
    print(f"# Revisión de {ST} con N≥{N_OBJETIVO}\n")
    print(f"_generado {datetime.now().isoformat(timespec='seconds')}; "
          f"corrector activo desde {desde}_\n")

    filas = []
    for dia, settle in cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
            "AND date>=? AND max_obs_f IS NOT NULL ORDER BY date",
            (ST, desde or "2026-08-28")):
        d = datetime.strptime(dia, "%Y-%m-%d").date()
        ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=HORA)
        lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
        fmt = "%Y-%m-%dT%H:%M:%S"
        r = an.execute(
            """SELECT ts, ens_med, bias_f, bias_applied FROM station_snapshots
               WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
               ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
            (ST, lo.strftime(fmt), hi.strftime(fmt),
             ref.astimezone(UTC).strftime(fmt))).fetchone()
        if r is None or not r["bias_applied"]:
            continue
        b = r["bias_f"] or 0.0
        bins = [(x[0], x[1]) for x in an.execute(
            """SELECT bin_lo, bin_hi FROM kalshi_snapshots
               WHERE station=? AND ts=(SELECT ts FROM kalshi_snapshots
                                       WHERE station=? AND ts<=?
                                       ORDER BY ts DESC LIMIT 1)""",
            (ST, ST, r["ts"]))]
        pub, sin = r["ens_med"], r["ens_med"] + b
        acierto_pub = acierto_sin = None
        if bins:
            bp, bs = bin_de(bins, pub), bin_de(bins, sin)
            bsettle = bin_de(bins, settle)
            acierto_pub = 1 if (bp is not None and bp == bsettle) else 0
            acierto_sin = 1 if (bs is not None and bs == bsettle) else 0
        filas.append({"dia": dia, "settle": settle, "pub": pub, "sin": sin,
                      "corr": b, "ap": acierto_pub, "as": acierto_sin})

    n = len(filas)
    if n == 0:
        print("Sin días evaluables todavía.")
        return 0

    e_pub = [abs(f["pub"] - f["settle"]) for f in filas]
    e_sin = [abs(f["sin"] - f["settle"]) for f in filas]
    m_pub, m_sin = statistics.mean(e_pub), statistics.mean(e_sin)
    acerta = sum(1 for a, b in zip(e_pub, e_sin) if a < b - 1e-9)
    no_nulos = sum(1 for a, b in zip(e_pub, e_sin) if abs(a - b) > 1e-9)
    p = p_signos_unilateral(acerta, no_nulos)
    con_bin = [f for f in filas if f["ap"] is not None]
    bin_pub = sum(f["ap"] for f in con_bin)
    bin_sin = sum(f["as"] for f in con_bin)

    print(f"| N | \\|err\\| publicado | sin corrector | mejora | acierta | "
          f"signos p | bin publicado | bin sin |")
    print("|---|---|---|---|---|---|---|---|")
    print(f"| {n} | {m_pub:.2f} | {m_sin:.2f} | {m_sin - m_pub:+.2f}°F | "
          f"{acerta}/{no_nulos} ({100*acerta/max(no_nulos,1):.0f}%) | "
          f"{p:.4f} | {bin_pub}/{len(con_bin)} | {bin_sin}/{len(con_bin)} |")

    ok_n = n >= N_OBJETIVO
    ok_a = (m_sin - m_pub) >= MEJORA_MIN_F
    ok_b = (acerta / max(no_nulos, 1)) >= ACIERTA_MIN_FRAC and p < 0.05
    ok_c = bin_pub >= bin_sin

    print("\n## Criterio (el de su corrida de entrada, sin rebajas)\n")
    print(f"- Muestra: **{n}** días (pide {N_OBJETIVO}) "
          f"{'✅' if ok_n else '⏳ faltan ' + str(N_OBJETIVO - n)}")
    print(f"- (a) mejora ≥{MEJORA_MIN_F}°F: **{m_sin - m_pub:+.2f}** "
          f"{'✅' if ok_a else '❌'}")
    print(f"- (b) acierta ≥{100*ACIERTA_MIN_FRAC:.0f}% con p<0.05: "
          f"**{100*acerta/max(no_nulos,1):.0f}%, p={p:.4f}** "
          f"{'✅' if ok_b else '❌'}")
    print(f"- (c) el acierto de bin no baja: **{bin_pub} vs {bin_sin}** "
          f"{'✅' if ok_c else '❌'}")

    print("\n## (d) ¿Se sostiene la ventana 9-13h?\n")
    r = an.execute(
        """SELECT COUNT(*) FROM station_snapshots
           WHERE station=? AND bias_frozen_f IS NOT NULL""", (ST,)).fetchone()
    print(f"Snapshots con la corrección registrada fuera de la ventana: "
          f"**{r[0]}** (se empezó a registrar el 2026-09-10).")
    if r[0] < 100:
        print("\nAún insuficiente. Cuando haya material, comparar el |err| a "
              "cada hora con y sin la corrección **que de verdad se habría "
              "aplicado** — no una réplica: el barrido del 08-28 se quedó sin "
              "poder decidir porque su réplica erraba 0.46°F sobre un efecto "
              "de 0.24.")

    print()
    if not ok_n:
        print(f"⏳ **AÚN NO DECIDE** — faltan {N_OBJETIVO - n} días.")
    elif ok_a and ok_b and ok_c:
        print("🟢 **SE QUEDA** — cumple el criterio que en agosto no cumplió.")
    else:
        print("🔴 **RETIRAR de ENABLED_STATIONS** — es lo que dijo su corrida "
              "de entrada y entonces se ignoró.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
