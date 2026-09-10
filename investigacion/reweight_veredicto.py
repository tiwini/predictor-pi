#!/usr/bin/env python3
"""Veredicto del reweight corregido por deff. Escrito el 2026-09-10 con CERO
días de sombra, para que el análisis quede pre-registrado igual que el criterio.

Criterio (DECISIONES.md, fila del 2026-09-10). ADOPTAR si las tres, con N≥10
días y decidiendo DENTRO de estación:

  (a) la banda alternativa queda MÁS CERCA del 80% nominal que la actual en
      ≥15 de 20 estaciones (test de signos, p=0.021)
  (b) el |err| de la mediana publicada NO empeora más de 0.10°F en el conjunto
  (c) cero violaciones nuevas del piso

RECHAZAR si (a) no llega, o si la mediana empeora ≥0.25°F.

Uso:  ./venv/bin/python3 ../investigacion/reweight_veredicto.py
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
from stations import STATION_TZ, PEAK_HOURS  # noqa: E402

UTC = ZoneInfo("UTC")
NOMINAL = 80.0
MIN_DIAS = 10
MIN_ESTACIONES_A_FAVOR = 15
TOLERANCIA_MEDIANA_F = 0.10
RECHAZO_MEDIANA_F = 0.25
VENTANA_MIN = 45


def p_signos(k, n):
    if n <= 0:
        return 1.0
    return sum(math.comb(n, i) for i in range(k, n + 1)) / (2 ** n)


def main():
    an = sqlite3.connect(f"file:{BASE / 'analysis.db'}?mode=ro", uri=True)
    an.row_factory = sqlite3.Row
    an.execute("PRAGMA busy_timeout=20000")
    cal = sqlite3.connect(f"file:{BASE / 'calibration.db'}?mode=ro", uri=True)

    print("# Veredicto — reweight corregido por deff\n")
    print(f"_generado {datetime.now().isoformat(timespec='seconds')}_\n")

    filas, err_pub_all, err_alt_all, violaciones = [], [], [], 0
    for st in sorted(STATION_TZ):
        if st not in PEAK_HOURS:
            continue
        tz = ZoneInfo(STATION_TZ[st])
        ref_h = PEAK_HOURS[st][0] - 2
        dias = cal.execute(
            "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
            "AND max_obs_f IS NOT NULL ORDER BY date", (st,)).fetchall()
        dentro_pub = dentro_alt = n = 0
        e_pub, e_alt = [], []
        for dia, settle in dias:
            d = datetime.strptime(dia, "%Y-%m-%d").date()
            ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=ref_h)
            lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            fmt = "%Y-%m-%dT%H:%M:%S"
            r = an.execute(
                """SELECT ens_med, ens_p10, ens_p90, ens_med_alt, ens_p10_alt,
                          ens_p90_alt, today_max_obs
                   FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_p10_alt IS NOT NULL
                   ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
                (st, lo.strftime(fmt), hi.strftime(fmt),
                 ref.astimezone(UTC).strftime(fmt))).fetchone()
            if r is None:
                continue
            n += 1
            if r["ens_p10"] - 1e-9 <= settle <= r["ens_p90"] + 1e-9:
                dentro_pub += 1
            if r["ens_p10_alt"] - 1e-9 <= settle <= r["ens_p90_alt"] + 1e-9:
                dentro_alt += 1
            e_pub.append(abs(r["ens_med"] - settle))
            e_alt.append(abs(r["ens_med_alt"] - settle))
            # (c) el piso: la rama alt no puede quedar por debajo de lo ya observado
            if (r["today_max_obs"] is not None
                    and r["ens_p10_alt"] < r["today_max_obs"] - 0.51):
                violaciones += 1
        if n == 0:
            continue
        cob_pub = 100.0 * dentro_pub / n
        cob_alt = 100.0 * dentro_alt / n
        mejora_cob = abs(cob_pub - NOMINAL) - abs(cob_alt - NOMINAL)
        filas.append({"st": st, "n": n, "cob_pub": cob_pub, "cob_alt": cob_alt,
                      "mejora": mejora_cob,
                      "err_pub": statistics.mean(e_pub),
                      "err_alt": statistics.mean(e_alt)})
        err_pub_all += e_pub
        err_alt_all += e_alt

    if not filas:
        print("**Sin datos de sombra todavía.** Vuelve cuando el poller lleve "
              "días liquidados con `ens_p10_alt` poblado.")
        return 0

    print("| est | N | cobertura ahora | con deff | ¿acerca al 80%? | "
          "\\|err\\| med ahora | con deff |")
    print("|---|---|---|---|---|---|---|")
    for f in filas:
        print(f"| {f['st']} | {f['n']} | {f['cob_pub']:.1f}% | {f['cob_alt']:.1f}% | "
              f"{'✅' if f['mejora'] > 0 else '❌'} {f['mejora']:+.1f}pp | "
              f"{f['err_pub']:.2f} | {f['err_alt']:.2f} |")

    n_dias = min(f["n"] for f in filas)
    a_favor = sum(1 for f in filas if f["mejora"] > 0)
    n_est = len(filas)
    m_pub = statistics.mean(err_pub_all)
    m_alt = statistics.mean(err_alt_all)
    delta_med = m_alt - m_pub
    p = p_signos(a_favor, n_est)

    print(f"\n## Criterio\n")
    ok_n = n_dias >= MIN_DIAS
    ok_a = a_favor >= MIN_ESTACIONES_A_FAVOR
    ok_b = delta_med <= TOLERANCIA_MEDIANA_F
    ok_c = violaciones == 0
    print(f"- Muestra: **{n_dias} días** mínimos por estación "
          f"(pide {MIN_DIAS}) {'✅' if ok_n else '⏳'}")
    print(f"- (a) acerca al 80% en **{a_favor} de {n_est}** estaciones "
          f"(pide {MIN_ESTACIONES_A_FAVOR}; signos p={p:.4f}) "
          f"{'✅' if ok_a else '❌'}")
    print(f"- (b) \\|err\\| de la mediana **{m_pub:.3f} → {m_alt:.3f}** "
          f"({delta_med:+.3f}°F; tolera +{TOLERANCIA_MEDIANA_F}) "
          f"{'✅' if ok_b else '❌'}")
    print(f"- (c) violaciones nuevas del piso: **{violaciones}** "
          f"{'✅' if ok_c else '❌'}")

    print()
    if not ok_n:
        print(f"⏳ **AÚN NO DECIDE** — faltan {MIN_DIAS - n_dias} días.")
    elif delta_med >= RECHAZO_MEDIANA_F:
        print(f"🔴 **RECHAZADO** — la mediana empeora {delta_med:+.3f}°F.")
    elif ok_a and ok_b and ok_c:
        print("🟢 **ADOPTAR** — se cumplen las tres condiciones.")
    else:
        print("🔴 **RECHAZADO** — no se cumplen las tres.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
