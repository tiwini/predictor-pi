"""Brier watchdog — F1 Fable audit response 2026-07-07.

Corre lunes 08:00 AST vía cron. Reporta Brier ours/Kalshi por estación en los
últimos 7 días settleados. Alert rojo (ntfy push) si una estación queda peor
calibrada que el mercado de forma material.

Motivación: `our_brier vs kalshi_brier` estuvo en day_summary desde abril
sin que nadie la mirara mientras el dashboard mostraba +53% ROI artefacto.
Este script fuerza el checkpoint semanal — vista rápida, umbral duro.

⚠ CAMBIO DE FUENTE 2026-09-10 (auditoría externa). Hasta hoy leía el Brier ya
agregado de `calibration.db.day_summary`, que lo calcula `compute_day_summary`
a partir de `market_cache.db.market_prices`. Y a esa tabla **sólo la escribe
`record_kalshi`**, al que llaman el web, la TUI y el CLI — nunca el
`analysis_poller`. O sea: cubría la estación que el usuario tuviera abierta.
Medido el 2026-09-10 sobre los 14 días anteriores: KPHX 14 días, KAUS 2, seis
estaciones con 1 día, y **las otras doce con cero**. De 20 filas diarias en
`day_summary`, entre 1 y 4 traían Brier. El 🔴 de KPHX que sonó tres semanas
seguidas no decía que KPHX fuera la peor: decía que era la única medida.

Es la tercera vez que muerde la misma asimetría — se llevó por delante al EWMA
(jubilado 2026-08-14) y tiene parado al bias tracker.

Ahora lee `analysis.db.kalshi_snapshots`, que sí escribe el poller para las 20,
con `our_p_calibrated` y `yes_mid` completos. Dos consecuencias:

  · **La serie nueva NO es comparable con la vieja.** La anterior promediaba
    todos los snapshots del día, tardes incluidas, donde acertar es fácil.
    Ésta toma **uno por día**, a la hora de referencia de la estación
    (`PEAK_HOURS[st][0] - 2`), la misma con la que se miden los backtests del
    corrector. Los Brier saldrán peores y eso no es una regresión.
  · Las filas quedan marcadas con `source` para no mezclarlas al comparar.

Cero coste, cero LLM. Solo query SQL + ntfy si aplica.
"""
from __future__ import annotations

import os
import sys
import sqlite3
import urllib.request
import urllib.error
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

UTC = ZoneInfo("UTC")

PROJECT_DIR = Path(__file__).resolve().parent
CALIBRATION_DB = PROJECT_DIR / "calibration.db"
ANALYSIS_DB = PROJECT_DIR / "analysis.db"
REVIEWS_DIR = Path.home() / "predictor-pi" / "brier_watchdog"
NTFY_ENV = Path.home() / ".config" / "ntfy.env"

BRIER_RATIO_ALERT_THR = 1.30
LOOKBACK_DAYS = 7

# Con la fuente vieja casi nunca había más de 2 o 3 días por estación, así que
# el mínimo estaba en 3. Desde el 2026-09-10 hay uno por día y por estación:
# pedir 5 de los 7 posibles descarta la semana rota sin dejar de vigilar.
ALERT_MIN_N = 5

# El ratio con el denominador cerca de cero no informa: la semana 2026-W37 dio
# **883×** para KBOS con n=1, que era un bin del mercado a 0.99 en un día ya
# resuelto. Por debajo de este Brier de Kalshi el ratio no se calcula y decide
# la diferencia absoluta, que sí es interpretable.
KALSHI_BRIER_MIN_FOR_RATIO = 0.010

# Y una diferencia mínima para alertar: 1.3× sobre un Brier de 0.02 es ruido.
BRIER_DIFF_ALERT_THR = 0.020

# Hora de referencia: la misma que usan los backtests del corrector y el
# seguimiento. Ventana de ±45 min para encontrar el snapshot más cercano.
REF_HOURS_BEFORE_PEAK = 2
REF_WINDOW_MIN = 45


def _load_ntfy_topic() -> str:
    if not NTFY_ENV.exists():
        return ""
    for ln in NTFY_ENV.read_text().splitlines():
        if ln.startswith("NTFY_TOPIC="):
            return ln.split("=", 1)[1].strip()
    return ""


def _push_ntfy(title: str, msg: str) -> bool:
    """True sólo si el push salió de verdad.

    Devolvía None y el caller imprimía "ntfy pushed" pase lo que pase, así que
    el log afirmaba envíos que no ocurrían: sin `~/.config/ntfy.env` esta
    función retorna en la primera línea y nadie se enteraba.
    """
    topic = _load_ntfy_topic()
    if not topic:
        print(f"[brier_watchdog] sin NTFY_TOPIC ({NTFY_ENV}), NO se empuja")
        return False
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={
                "Title": title,
                "Priority": "high",
                "Tags": "warning,chart_with_downwards_trend",
            },
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.URLError as e:
        print(f"[brier_watchdog] ntfy push failed: {e}", file=sys.stderr)
        return False


def _ensure_brier_weekly_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS brier_weekly (
            week_iso TEXT NOT NULL,
            station_id TEXT NOT NULL,
            generated_at TEXT NOT NULL,
            lookback_days INTEGER NOT NULL,
            n INTEGER NOT NULL,
            our_brier REAL,
            kalshi_brier REAL,
            ratio REAL,
            alerted INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (week_iso, station_id)
        )""")
    # 2026-09-10: `diff` porque el ratio no informa con el denominador cerca de
    # cero, y `source` para no comparar la serie nueva con la vieja sin darse
    # cuenta — miden cosas distintas (ver docstring del módulo).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(brier_weekly)")}
    for col, tipo in (("diff", "REAL"), ("source", "TEXT")):
        if col not in cols:
            conn.execute(f"ALTER TABLE brier_weekly ADD COLUMN {col} {tipo}")
    conn.commit()


SOURCE_TAG = "analysis_snapshots"


def es_roja(s: dict, alert_thr: float = BRIER_RATIO_ALERT_THR) -> bool:
    """¿Alerta esta estación? Fuente ÚNICA del criterio.

    Estaba duplicado entre `render_markdown` y `main`, con el detalle de que
    el render marcaba 🔴 sin mirar `n` y luego `main` sí lo miraba: el informe
    decía «ALERT» de estaciones que no se empujaban. Las tres condiciones:

      1. muestra suficiente (n ≥ ALERT_MIN_N),
      2. diferencia material (peor que el mercado en ≥ 0.020 de Brier), y
      3. el ratio por encima del umbral **cuando el ratio significa algo**.

    (3) se salta si el Brier de Kalshi es minúsculo: ahí manda (2).
    """
    if s["n"] < ALERT_MIN_N:
        return False
    if s.get("diff") is None or s["diff"] < BRIER_DIFF_ALERT_THR:
        return False
    if s["ratio"] is None:
        return True
    return s["ratio"] > alert_thr


def persist_weekly(stats: list[dict], week_iso: str,
                    alert_thr: float = BRIER_RATIO_ALERT_THR) -> None:
    conn = sqlite3.connect(CALIBRATION_DB)
    try:
        _ensure_brier_weekly_table(conn)
        ts = datetime.now().isoformat(timespec="seconds")
        for s in stats:
            conn.execute(
                """INSERT OR REPLACE INTO brier_weekly
                   (week_iso, station_id, generated_at, lookback_days, n,
                    our_brier, kalshi_brier, ratio, alerted, diff, source)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (week_iso, s["station_id"], ts, LOOKBACK_DAYS, s["n"],
                 s["our_brier"], s["kalshi_brier"], s["ratio"],
                 1 if es_roja(s, alert_thr) else 0, s.get("diff"), SOURCE_TAG))
        conn.commit()
    finally:
        conn.close()


def _bin_contains(settle: float, lo: float, hi: float) -> int:
    """1 si el settle cae en el bin, con el ±0.5 del redondeo del NWS.

    El NWS liquida en °F entero y `our_p_for_bin` cubre [lo-0.5, hi+0.5]; el
    outcome tiene que usar el mismo criterio o el Brier mide otra pregunta.
    Los bins de cola llegan como ±1e9 desde la DB (en código son ±inf).
    """
    LO = lo if abs(lo) < 1e8 else float("-inf")
    HI = hi if abs(hi) < 1e8 else float("inf")
    return 1 if (LO - 0.5) <= settle <= (HI + 0.5) else 0


def _brier_del_dia(an: sqlite3.Connection, station: str, day: str,
                   settle: float, tz, ref_hour: int) -> tuple | None:
    """(our_brier, kalshi_brier, n_bins) de UN snapshot, o None.

    Un solo snapshot por día, el más cercano a la hora de referencia. Promediar
    el día entero premiaría las tardes, cuando el máximo ya está puesto y
    acertar no tiene mérito — el mismo defecto que hace que `eff_N` parezca una
    señal cuando sólo marca la hora.
    """
    d = datetime.strptime(day, "%Y-%m-%d").date()
    ref = datetime.combine(d, datetime.min.time(), tz) + timedelta(hours=ref_hour)
    lo = (ref - timedelta(minutes=REF_WINDOW_MIN)).astimezone(UTC)
    hi = (ref + timedelta(minutes=REF_WINDOW_MIN)).astimezone(UTC)
    fmt = "%Y-%m-%dT%H:%M:%S"
    r = an.execute(
        """SELECT ts FROM kalshi_snapshots
           WHERE station=? AND ts>=? AND ts<=? AND yes_mid IS NOT NULL
           ORDER BY ABS(JULIANDAY(ts) - JULIANDAY(?)) LIMIT 1""",
        (station, lo.strftime(fmt), hi.strftime(fmt),
         ref.astimezone(UTC).strftime(fmt))).fetchone()
    if r is None:
        return None
    bins = an.execute(
        """SELECT bin_lo, bin_hi, yes_mid,
                  COALESCE(our_p_calibrated, our_p) AS p
           FROM kalshi_snapshots
           WHERE station=? AND ts=? AND yes_mid IS NOT NULL
             AND COALESCE(our_p_calibrated, our_p) IS NOT NULL""",
        (station, r[0])).fetchall()
    if not bins:
        return None
    sse_o = sse_k = 0.0
    for blo, bhi, ym, p in bins:
        outc = _bin_contains(settle, blo, bhi)
        sse_o += (p - outc) ** 2
        sse_k += (ym - outc) ** 2
    n = len(bins)
    return sse_o / n, sse_k / n, n


def compute_brier_by_station(days: int = LOOKBACK_DAYS) -> list[dict]:
    """Brier por estación sobre los días liquidados de la ventana.

    Fuente: `analysis.db.kalshi_snapshots` (las 20 estaciones) + el settle del
    CLI en `day_outcomes`. Ver el cambio de fuente en el docstring del módulo.
    """
    from stations import STATION_TZ, PEAK_HOURS
    since = (date.today() - timedelta(days=days)).isoformat()
    cal = sqlite3.connect(f"file:{CALIBRATION_DB}?mode=ro", uri=True)
    an = sqlite3.connect(f"file:{ANALYSIS_DB}?mode=ro", uri=True)
    an.execute("PRAGMA busy_timeout=20000")
    out = []
    try:
        dias = cal.execute(
            """SELECT station_id, date, max_obs_f FROM day_outcomes
               WHERE date >= ? AND max_obs_f IS NOT NULL
               ORDER BY station_id, date""", (since,)).fetchall()
        por_est: dict[str, list] = {}
        for sid, day, settle in dias:
            if sid not in STATION_TZ or sid not in PEAK_HOURS:
                continue
            por_est.setdefault(sid, []).append((day, float(settle)))

        for sid in sorted(por_est):
            tz = ZoneInfo(STATION_TZ[sid])
            ref_hour = PEAK_HOURS[sid][0] - REF_HOURS_BEFORE_PEAK
            ours, kals = [], []
            for day, settle in por_est[sid]:
                res = _brier_del_dia(an, sid, day, settle, tz, ref_hour)
                if res is None:
                    continue
                ours.append(res[0])
                kals.append(res[1])
            if not ours:
                continue
            our = sum(ours) / len(ours)
            k = sum(kals) / len(kals)
            ratio = (our / k) if k >= KALSHI_BRIER_MIN_FOR_RATIO else None
            out.append({
                "station_id": sid, "n": len(ours),
                "our_brier": our, "kalshi_brier": k, "ratio": ratio,
                "diff": our - k,
            })
    finally:
        cal.close()
        an.close()
    return out


def render_markdown(stats: list[dict], week_iso: str) -> str:
    lines = [
        f"# Brier watchdog — semana ISO {week_iso}",
        f"_generado {datetime.now().isoformat(timespec='seconds')}, "
        f"lookback {LOOKBACK_DAYS}d · un snapshot por día a la hora de "
        f"referencia · alerta si n≥{ALERT_MIN_N}, diff≥{BRIER_DIFF_ALERT_THR:.3f} "
        f"y ratio>{BRIER_RATIO_ALERT_THR}_",
        "",
        "| station | n | our Brier | Kalshi Brier | diff | ratio | alert |",
        "|---------|---|-----------|--------------|------|-------|-------|",
    ]
    alerted: list[str] = []
    for s in stats:
        r = s["ratio"]
        r_str = f"{r:.2f}×" if r is not None else "—"
        if es_roja(s):
            flag = "🔴"
            alerted.append(s["station_id"])
        elif s["n"] < ALERT_MIN_N:
            flag = "🟡 low-N"
        elif s.get("diff") is not None and s["diff"] > 0:
            flag = "🟠 peor"        # peor que el mercado, sin llegar al umbral
        else:
            flag = "🟢"
        our_s = f"{s['our_brier']:.3f}" if s['our_brier'] is not None else "—"
        k_s = f"{s['kalshi_brier']:.3f}" if s['kalshi_brier'] is not None else "—"
        d_s = f"{s['diff']:+.3f}" if s.get("diff") is not None else "—"
        lines.append(
            f"| {s['station_id']} | {s['n']} | {our_s} | {k_s} | {d_s} | "
            f"{r_str} | {flag} |"
        )

    lines.extend(["", ""])
    n_est = len(stats)
    if alerted:
        lines.append(
            f"## 🔴 ALERT: {len(alerted)} de {n_est} estaciones peor "
            f"calibradas que el mercado: {', '.join(alerted)}"
        )
        lines.append(
            "Brier nuestro > el de Kalshi de forma material y sostenida = "
            "margen de mejora sistemático — revisar reliability curve "
            "(`isotonic.reliability_curve()`) y el corrector de nivel."
        )
    else:
        lines.append(f"## 🟢 OK: ninguna de las {n_est} estaciones alerta.")
    lines.append("")
    lines.append(
        f"_Fuente: `analysis.db.kalshi_snapshots` ({n_est} estaciones). "
        "Hasta el 2026-09-10 se leía `day_summary`, que sólo cubría la "
        "estación abierta en el web — no comparar las dos series._")

    return "\n".join(lines) + "\n"


def main(dry_run: bool = False) -> None:
    stats = compute_brier_by_station()
    if not stats:
        print("[brier_watchdog] sin data para lookback — skip", file=sys.stderr)
        return

    # %G-W%V: ISO year to match ISO week; %Y en enero puede desalinear.
    week_iso = date.today().strftime("%G-W%V")
    md = render_markdown(stats, week_iso)

    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    outfile = REVIEWS_DIR / f"brier_{week_iso}.md"
    if dry_run:
        print(f"[brier_watchdog] --dry-run: rendering only, skip persist/push")
        print(md)
    else:
        outfile.write_text(md, encoding="utf-8")
        persist_weekly(stats, week_iso)
        print(f"[brier_watchdog] escrito {outfile} + tabla brier_weekly")

    alerted = [s["station_id"] for s in stats if es_roja(s)]
    if alerted:
        title = f"Brier watchdog: {len(alerted)}/{len(stats)} estaciones over"
        body = (
            f"Peor que el mercado (n≥{ALERT_MIN_N}, diff≥"
            f"{BRIER_DIFF_ALERT_THR:.3f}): {', '.join(alerted)}\nVer {outfile}"
        )
        if dry_run:
            print(f"[brier_watchdog] --dry-run: would push ntfy → {title}: {body}")
        else:
            if _push_ntfy(title, body):
                print(f"[brier_watchdog] ntfy pushed for {alerted}")
            else:
                print(f"[brier_watchdog] alerta NO entregada para {alerted}")
    else:
        print("[brier_watchdog] no alert this week")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
