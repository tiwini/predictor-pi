#!/usr/bin/env python3
"""(d) de la revision de KLAS: se sostiene la ventana 9-13h?

El barrido del 2026-08-28 eligio 9-13h sobre la misma muestra que acababa de
fallar, y con una REPLICA de la correccion que erraba 0.46 F sobre un efecto de
0.24. Desde el 2026-09-10 el poller registra en `bias_frozen_f` la correccion
que de verdad se habria aplicado fuera de la ventana, ya capeada por el piso.
Esto usa esa sombra: nada de replica.

Convenio de signos tomado de seguimiento_corrector.fila_del_dia:
    aplicado  (bias_applied=1): pub = ens_med          sin = ens_med + bias_f
    sombra    (bias_applied=0): sin = ens_med          con = ens_med - bias_frozen_f
"""
import sqlite3
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))
from stations import STATION_TZ  # noqa: E402

ST = "KLAS"
UTC = ZoneInfo("UTC")
VENTANA_MIN = 45
FMT = "%Y-%m-%dT%H:%M:%S"


def main():
    an = sqlite3.connect("file:%s?mode=ro" % (BASE / "analysis.db"), uri=True)
    an.row_factory = sqlite3.Row
    cal = sqlite3.connect("file:%s?mode=ro" % (BASE / "calibration.db"),
                          uri=True)
    tz = ZoneInfo(STATION_TZ[ST])

    dias = [(r[0], r[1]) for r in cal.execute(
        "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
        "AND max_obs_f IS NOT NULL AND date>='2026-09-10' ORDER BY date",
        (ST,))]

    print("# (d) Ventana horaria de KLAS contra la sombra real\n")
    print("_%d dias con settle desde 2026-09-10_\n" % len(dias))
    print("| hora | N | modo | |err| con corr | |err| sin corr | mejora | "
          "ayuda |")
    print("|---|---|---|---|---|---|---|")

    resumen = {}
    for hora in range(6, 21):
        con, sin, modos = [], [], set()
        for dia, settle in dias:
            d = datetime.strptime(dia, "%Y-%m-%d").date()
            ref = datetime.combine(d, datetime.min.time(), tz) \
                + timedelta(hours=hora)
            lo = (ref - timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            hi = (ref + timedelta(minutes=VENTANA_MIN)).astimezone(UTC)
            r = an.execute(
                """SELECT ens_med, bias_f, bias_applied, bias_frozen_f
                   FROM station_snapshots
                   WHERE station=? AND ts>=? AND ts<=? AND ens_med IS NOT NULL
                   ORDER BY ABS(JULIANDAY(ts)-JULIANDAY(?)) LIMIT 1""",
                (ST, lo.strftime(FMT), hi.strftime(FMT),
                 ref.astimezone(UTC).strftime(FMT))).fetchone()
            if r is None:
                continue
            if r["bias_applied"]:
                b = r["bias_f"] or 0.0
                c_con, c_sin = r["ens_med"], r["ens_med"] + b
                modos.add("aplicado")
            elif r["bias_frozen_f"] is not None:
                b = r["bias_frozen_f"]
                c_con, c_sin = r["ens_med"] - b, r["ens_med"]
                modos.add("sombra")
            else:
                continue
            con.append(abs(c_con - settle))
            sin.append(abs(c_sin - settle))

        if len(con) < 10:
            continue
        mc, ms = statistics.mean(con), statistics.mean(sin)
        ayuda = sum(1 for a, b in zip(con, sin) if a < b - 1e-9)
        nn = sum(1 for a, b in zip(con, sin) if abs(a - b) > 1e-9)
        modo = "+".join(sorted(modos))
        dentro = 9 <= hora <= 13
        resumen[hora] = (mc, ms, ms - mc, ayuda, nn, dentro)
        print("| %2dh%s | %d | %s | %.2f | %.2f | %+.2f | %d/%d (%.0f%%) |" % (
            hora, " **·**" if dentro else "", len(con), modo, mc, ms, ms - mc,
            ayuda, nn, 100 * ayuda / max(nn, 1)))

    print("\n**·** = dentro de la ventana 9-13h vigente.\n")
    dentro = [v for h, v in resumen.items() if v[5]]
    fuera = [v for h, v in resumen.items() if not v[5]]
    if dentro and fuera:
        print("- Dentro de 9-13h: mejora media **%+.2f F**" %
              statistics.mean(v[2] for v in dentro))
        print("- Fuera de 9-13h: mejora media **%+.2f F**" %
              statistics.mean(v[2] for v in fuera))
    buenas = sorted(h for h, v in resumen.items() if v[2] > 0)
    if buenas:
        print("- Horas con mejora positiva: **%s**" %
              ", ".join("%dh" % h for h in buenas))
    malas = sorted(h for h, v in resumen.items() if v[2] <= 0)
    if malas:
        print("- Horas donde estorba o da igual: **%s**" %
              ", ".join("%dh" % h for h in malas))
    return 0


if __name__ == "__main__":
    sys.exit(main())
