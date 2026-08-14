"""Techo físico del día: lo que el termómetro ya no permite alcanzar.

Por qué existe
--------------
El gate de bets marcaba operaciones ACTIONABLE que contradecían la observación,
y perdía. Cuatro casos con análisis en vivo (ver
`investigacion/caso_knyc_2026_08_12.md`):

    08-07 KATL  comprar 89-90 (+26.5pp)  con el pico CONFIRMED en 87.1  -> perdía
    08-07 KLAS  vender 110-111 (+41.2pp) con la ventana abierta 2.6 h   -> ACERTABA
    08-10 KSEA  vender "75 or below" y comprar 80-81, current 14°F bajo -> perdían
    08-12 KNYC  comprar 87-88 (+45.4pp)  con 82.9 plano 1h07m           -> perdía

El patrón: `our_p` sale del ensemble y el ensemble no mira el termómetro. Cuando
el día ya no puede llegar a un bin, ningún edge sobre ese bin es real.

KLAS es el contraejemplo que fija el límite: allí la ventana seguía abierta 2.6 h
y el "físico" era una proyección estadística, no una observación. Por eso este
módulo **sólo acota cuando hay observación que lo respalde**, y devuelve None en
caso contrario — prefiere no bloquear a bloquear de más.

Qué hace
--------
`ceiling_f()` devuelve (techo_°F, razón) o (None, razón) si el día aún no se
puede acotar. Tres vías, de más dura a más blanda:

  1. pico CONFIRMED, o ventana de pico ya cerrada
       -> techo = max_obs + CLI_GAP_F   (el CLI capta picos que el feed no ve)
  2. temperatura plana >= FLAT_MIN y queda poca ventana
       -> techo = max_obs + FLAT_HEADROOM_F
  3. resto: techo = max_obs + p90 histórico de la subida restante a esa hora
       -> None si no hay historia suficiente

Es deliberadamente conservador: el p90 deja fuera sólo lo que el 90% de los días
no logró.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger("physical_gate")

DB_PATH = Path(__file__).parent / "analysis.db"
CAL_DB_PATH = Path(__file__).parent / "calibration.db"

# El CLI usa ASOS 1-min y capta picos que el feed de 5-min pierde. Medido en
# KATL: gap mediano +0.0, p90 +1.0. Se usa el p90 para no bloquear de más.
CLI_GAP_F = 1.0

# "Plana" = sin subir en este tiempo, con la ventana casi agotada.
FLAT_MIN = 60
FLAT_WINDOW_LEFT_H = 1.5
FLAT_HEADROOM_F = 2.0

MIN_DAYS_HIST = 12
_p90_cache: dict[tuple[str, int, str], Optional[float]] = {}


def _p90_subida_restante(station_id: str, hora_local: int,
                         hoy: str) -> Optional[float]:
    """p90 de (settle - current) a esa hora local, sólo con días ANTERIORES."""
    key = (station_id, hora_local, hoy)
    if key in _p90_cache:
        return _p90_cache[key]
    out: Optional[float] = None
    try:
        from stations import STATION_TZ
        tz = ZoneInfo(STATION_TZ[station_id])
        cal = sqlite3.connect(f"file:{CAL_DB_PATH}?mode=ro", uri=True)
        try:
            dias = cal.execute(
                "SELECT date, max_obs_f FROM day_outcomes WHERE station_id=? "
                "AND date < ? AND max_obs_f IS NOT NULL "
                "ORDER BY date DESC LIMIT 40", (station_id, hoy)).fetchall()
        finally:
            cal.close()
        an = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
        an.execute("PRAGMA busy_timeout=5000")
        try:
            subidas = []
            for d, settle in dias:
                ref = (datetime.combine(datetime.strptime(d, "%Y-%m-%d").date(),
                                        datetime.min.time(), tz)
                       + timedelta(hours=hora_local))
                a = (ref - timedelta(minutes=20)).astimezone(timezone.utc)
                b = (ref + timedelta(minutes=20)).astimezone(timezone.utc)
                r = an.execute(
                    """SELECT current_f FROM station_snapshots
                       WHERE station=? AND ts>=? AND ts<=? AND current_f IS NOT NULL
                       ORDER BY ts LIMIT 1""",
                    (station_id, a.strftime("%Y-%m-%dT%H:%M:%S"),
                     b.strftime("%Y-%m-%dT%H:%M:%S"))).fetchone()
                if r is not None:
                    subidas.append(settle - r[0])
        finally:
            an.close()
        if len(subidas) >= MIN_DAYS_HIST:
            subidas.sort()
            out = subidas[int(0.9 * len(subidas))]
    except Exception as e:
        log.warning("p90 subida %s h=%s falló: %s", station_id, hora_local, e)
        out = None
    _p90_cache[key] = out
    return out


def clear_cache() -> None:
    _p90_cache.clear()


def ceiling_f(station_id: str, snap) -> tuple[Optional[float], str]:
    """(techo plausible en °F, razón). (None, motivo) si no se puede acotar."""
    try:
        from stations import PEAK_HOURS
        max_obs = getattr(snap, "today_max_obs", None)
        if max_obs is None or max_obs <= -900:
            return None, "sin max_obs"
        # El feed de 5 min va por delante del METAR horario; se toma el mayor
        # para no acotar por debajo de lo ya medido.
        cur = getattr(snap, "current_temp_f", None)
        base = max(max_obs, cur) if cur is not None and cur > -900 else max_obs

        local = getattr(snap, "station_local", None)
        if local is None:
            return None, "sin hora local"
        hora_f = local.hour + local.minute / 60.0
        lo_p, hi_p = PEAK_HOURS[station_id]

        peak_state = getattr(snap, "peak_state", None)
        confirmado = peak_state is not None and "CONFIRM" in str(peak_state).upper()

        # ── vía 1: el pico ya está puesto ──────────────────────────────
        if confirmado:
            return base + CLI_GAP_F, f"pico confirmado (max {base:.1f})"
        if hora_f >= hi_p:
            return base + CLI_GAP_F, f"ventana de pico cerrada (max {base:.1f})"

        # ── vía 2: plana y con la ventana agotándose ───────────────────
        estable = getattr(snap, "current_temp_stable_min", None)
        queda = hi_p - hora_f
        if (estable is not None and estable >= FLAT_MIN
                and 0 < queda <= FLAT_WINDOW_LEFT_H):
            return (base + FLAT_HEADROOM_F,
                    f"{estable} min plana y {queda:.1f} h de ventana")

        # ── vía 3: p90 histórico de subida restante ────────────────────
        p90 = _p90_subida_restante(station_id, local.hour,
                                   local.date().isoformat())
        if p90 is None:
            return None, "sin historia suficiente para acotar"
        return base + p90, f"p90 de subida restante a las {local.hour}h (+{p90:.1f})"
    except Exception as e:
        log.warning("ceiling_f %s falló: %s", station_id, e)
        return None, f"error: {e}"


def blocks_bin(side: str, bin_lo: float, bin_hi: float,
               ceiling: Optional[float]) -> Optional[str]:
    """Razón por la que el techo físico veta esta operación, o None.

    Dos vetos distintos, uno por cada error observado:

      YES sobre un bin cuyo SUELO ya está por encima del techo — el bin no
          puede ganar (KATL 89-90, KNYC 87-88, KSEA 80-81).
      NO  sobre el bin donde CAE el techo — es el desenlace más probable, así
          que venderlo es apostar contra la observación (KSEA "75 or below").

    El redondeo sigue la convención del resto del sistema: el bin cubre
    [lo-0.5, hi+0.5] porque el settle del NWS es entero.
    """
    if ceiling is None:
        return None
    if side == "YES" and bin_lo > -1e8 and (bin_lo - 0.5) > ceiling:
        return (f"techo físico {ceiling:.1f}°F por debajo del suelo del bin "
                f"({bin_lo:.0f}°)")
    if side == "NO":
        dentro = (((bin_lo - 0.5) <= ceiling if bin_lo > -1e8 else True)
                  and (ceiling <= (bin_hi + 0.5) if bin_hi < 1e8 else True))
        if dentro:
            return f"techo físico {ceiling:.1f}°F cae DENTRO del bin"
    return None


def _estable_min_desde_db(con, station_id: str, cur: Optional[float]) -> Optional[int]:
    """Minutos que `current_f` lleva sin cambiar, leídos de la serie guardada.

    Se calcula de los datos en vez de parsear `narrative_line`, que es texto de
    presentación y puede cambiar de formato.
    """
    if cur is None:
        return None
    try:
        filas = con.execute(
            """SELECT ts, current_f FROM station_snapshots
               WHERE station=? AND current_f IS NOT NULL
               ORDER BY ts DESC LIMIT 40""", (station_id,)).fetchall()
        if not filas:
            return None
        ult = datetime.fromisoformat(filas[0][0])
        desde = ult
        for ts, c in filas:
            if abs((c or 0) - cur) > 0.05:
                break
            desde = datetime.fromisoformat(ts)
        return int((ult - desde).total_seconds() / 60)
    except Exception:
        return None


def ceiling_from_db(station_id: str, con, row) -> tuple[Optional[float], str]:
    """Igual que `ceiling_f` pero desde una fila de `station_snapshots`.

    Lo usan las herramientas de lectura, que trabajan sobre la DB en vez de
    construir un Snapshot. `row` debe traer today_max_obs, current_f y
    peak_status.
    """
    try:
        from stations import STATION_TZ
        def g(k):
            try:
                return row[k]
            except (IndexError, KeyError):
                return None
        cur = g("current_f")
        # Preferir el valor persistido (serie METAR); el derivado de los
        # snapshots del poller pierde resolución por la cadencia de 10 min.
        estable = g("current_stable_min")
        local = datetime.now(ZoneInfo(STATION_TZ[station_id]))
        # `peak_status` es el texto de display; CONFIRMED se muestra con 🔒.
        ps = (g("peak_status") or "")
        peak = "PeakState.CONFIRMED" if ("confirmado" in ps or "🔒" in ps) else ps
        snap = type("_S", (), {
            "today_max_obs": g("today_max_obs"),
            "current_temp_f": cur,
            "peak_state": peak,
            "current_temp_stable_min": (
                estable if estable is not None
                else _estable_min_desde_db(con, station_id, cur)),
            "station_local": local,
        })()
        return ceiling_f(station_id, snap)
    except Exception as e:
        log.warning("ceiling_from_db %s falló: %s", station_id, e)
        return None, f"error: {e}"
