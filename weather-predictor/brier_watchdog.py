"""Brier watchdog — F1 Fable audit response 2026-07-07.

Corre lunes 08:00 AST vía cron. Reporta Brier ratio ours/Kalshi por estación
en los últimos 7 días settleados. Alert rojo (ntfy push) si algún ratio > 1.3.

Motivación: `our_brier vs kalshi_brier` estuvo en day_summary desde abril
sin que nadie la mirara mientras el dashboard mostraba +53% ROI artefacto.
Este script fuerza el checkpoint semanal — vista rápida, umbral duro.

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

PROJECT_DIR = Path(__file__).resolve().parent
CALIBRATION_DB = PROJECT_DIR / "calibration.db"
REVIEWS_DIR = Path.home() / "predictor-pi" / "brier_watchdog"
NTFY_ENV = Path.home() / ".config" / "ntfy.env"

BRIER_RATIO_ALERT_THR = 1.30
LOOKBACK_DAYS = 7
ALERT_MIN_N = 3  # requerimos ≥3 días settleados para alertar; Fable audit 2026-07-09


def _load_ntfy_topic() -> str:
    if not NTFY_ENV.exists():
        return ""
    for ln in NTFY_ENV.read_text().splitlines():
        if ln.startswith("NTFY_TOPIC="):
            return ln.split("=", 1)[1].strip()
    return ""


def _push_ntfy(title: str, msg: str) -> None:
    topic = _load_ntfy_topic()
    if not topic:
        return
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
    except urllib.error.URLError as e:
        print(f"[brier_watchdog] ntfy push failed: {e}", file=sys.stderr)


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
    conn.commit()


def persist_weekly(stats: list[dict], week_iso: str,
                    alert_thr: float = BRIER_RATIO_ALERT_THR) -> None:
    conn = sqlite3.connect(CALIBRATION_DB)
    try:
        _ensure_brier_weekly_table(conn)
        ts = datetime.now().isoformat(timespec="seconds")
        for s in stats:
            alerted = 1 if (s["ratio"] is not None
                            and s["ratio"] > alert_thr) else 0
            conn.execute(
                """INSERT OR REPLACE INTO brier_weekly
                   (week_iso, station_id, generated_at, lookback_days, n,
                    our_brier, kalshi_brier, ratio, alerted)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (week_iso, s["station_id"], ts, LOOKBACK_DAYS, s["n"],
                 s["our_brier"], s["kalshi_brier"], s["ratio"], alerted))
        conn.commit()
    finally:
        conn.close()


def compute_brier_by_station(days: int = LOOKBACK_DAYS) -> list[dict]:
    since = (date.today() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(CALIBRATION_DB)
    try:
        rows = conn.execute(
            """SELECT station_id, COUNT(*) AS n,
                      AVG(our_brier) AS our_avg,
                      AVG(kalshi_brier) AS k_avg
               FROM day_summary
               WHERE date >= ?
                 AND our_brier IS NOT NULL
                 AND kalshi_brier IS NOT NULL
               GROUP BY station_id
               ORDER BY station_id""",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    out = []
    for sid, n, our, k in rows:
        ratio = (our / k) if k and k > 0 else None
        out.append({
            "station_id": sid, "n": n,
            "our_brier": our, "kalshi_brier": k, "ratio": ratio,
        })
    return out


def render_markdown(stats: list[dict], week_iso: str) -> str:
    lines = [
        f"# Brier watchdog — semana ISO {week_iso}",
        f"_generado {datetime.now().isoformat(timespec='seconds')}, "
        f"lookback {LOOKBACK_DAYS}d, umbral alert ratio > {BRIER_RATIO_ALERT_THR}_",
        "",
        "| station | n | our Brier | Kalshi Brier | ratio | alert |",
        "|---------|---|-----------|--------------|-------|-------|",
    ]
    alerted: list[str] = []
    for s in stats:
        r = s["ratio"]
        if r is None:
            flag = "—"
            r_str = "—"
        else:
            r_str = f"{r:.2f}×"
            over = r > BRIER_RATIO_ALERT_THR
            low_n = s["n"] < ALERT_MIN_N
            if over and low_n:
                flag = "🟡 low-N"
            elif over:
                flag = "🔴"
                alerted.append(s["station_id"])
            else:
                flag = "🟢"
        our_s = f"{s['our_brier']:.3f}" if s['our_brier'] is not None else "—"
        k_s = f"{s['kalshi_brier']:.3f}" if s['kalshi_brier'] is not None else "—"
        lines.append(
            f"| {s['station_id']} | {s['n']} | {our_s} | {k_s} | {r_str} | {flag} |"
        )

    lines.extend(["", ""])
    if alerted:
        lines.append(
            f"## 🔴 ALERT: {len(alerted)} estaciones con Brier ratio > "
            f"{BRIER_RATIO_ALERT_THR}: {', '.join(alerted)}"
        )
        lines.append(
            "Ratio > 1 = nuestro modelo peor calibrado que Kalshi. "
            "Ratio > 1.3 sostenido = margen de mejora sistemático — revisar "
            "reliability curve (`isotonic.reliability_curve()`) y bias tracker."
        )
    else:
        lines.append(
            "## 🟢 OK: ninguna estación con Brier ratio > "
            f"{BRIER_RATIO_ALERT_THR}."
        )

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

    alerted = [s["station_id"] for s in stats
               if s["ratio"] is not None
               and s["ratio"] > BRIER_RATIO_ALERT_THR
               and s["n"] >= ALERT_MIN_N]
    if alerted:
        title = f"Brier watchdog: {len(alerted)}/{len(stats)} estaciones over"
        body = (
            f"Ratio > {BRIER_RATIO_ALERT_THR} (n≥{ALERT_MIN_N}): "
            f"{', '.join(alerted)}\nVer {outfile}"
        )
        if dry_run:
            print(f"[brier_watchdog] --dry-run: would push ntfy → {title}: {body}")
        else:
            _push_ntfy(title, body)
            print(f"[brier_watchdog] ntfy pushed for {alerted}")
    else:
        print("[brier_watchdog] no alert this week")


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    main(dry_run=dry)
