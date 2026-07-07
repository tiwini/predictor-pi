"""Digest semanal de guard EV — forcing function empujada (fable 2026-07-03).

Corre domingos 8:15 AM AST vía cron. Para cada estación, ejecuta
`agent_signals.guard_ev` sobre `simulated_bets` y aplica
`guard_relax_candidate` a cada guard. Escribe
`~/predictor-pi/guard_reviews/guard_review_YYYY-WW.md` con:
  - Bucket `sole` (guard fue único bloqueador ex-ante) y `shared` por guard.
  - Verdict de relajación con la regla asimétrica (N≥40 + ROI>0 + trim).
  - Delta vs semana anterior si el archivo previo existe.

Push ntfy sólo si algún candidato NUEVO transiciona a True — evita ruido.

Sin LLM: markdown pre-armado, cero coste. Sólo lectura de calibration.db.
"""
from __future__ import annotations

import os
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

import agent_signals as sig
from stations import STATION_IDS

CALIBRATION_DB = PROJECT_DIR / "calibration.db"
REVIEWS_DIR = Path.home() / "predictor-pi" / "guard_reviews"
NTFY_ENV = Path.home() / ".config" / "ntfy.env"


def _load_ntfy_topic() -> str:
    if not NTFY_ENV.exists():
        return ""
    for ln in NTFY_ENV.read_text().splitlines():
        if ln.startswith("NTFY_TOPIC="):
            return ln.split("=", 1)[1].strip()
    return ""


def _push_ntfy(msg: str) -> None:
    topic = _load_ntfy_topic()
    if not topic:
        return
    try:
        req = urllib.request.Request(
            f"https://ntfy.sh/{topic}",
            data=msg.encode("utf-8"),
            headers={"Title": "guard digest", "Priority": "default"},
        )
        urllib.request.urlopen(req, timeout=10).read()
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"ntfy fail: {e}", file=sys.stderr)


def _fmt_bucket(b: dict) -> str:
    n = b.get("trades", 0)
    if n == 0:
        return "n=0"
    return (f"n={n} w={b.get('wins',0)} pl=${b.get('pl',0.0):+.2f} "
            f"ROI={b.get('roi_pct',0.0):+.1f}%")


def _row_for_guard(label: str, slot: dict):
    verdict = sig.guard_relax_candidate(slot)
    flag = "RELAX?" if verdict["candidate"] else "keep"
    row = (f"| {label} | {_fmt_bucket(slot['sole'])} | "
           f"{_fmt_bucket(slot['shared'])} | {flag} | {verdict['reason']} |")
    return row, verdict["candidate"]


def _prev_verdicts(prev_path: Path) -> dict:
    """Extrae candidatos True del archivo semana previa (parse laxo).

    Devuelve {(station, guard): True} para saber si un flag ES nuevo o ya
    estaba disparado la semana pasada.
    """
    prev: dict = {}
    if not prev_path.exists():
        return prev
    station: str | None = None
    for ln in prev_path.read_text().splitlines():
        s = ln.strip()
        if s.startswith("## "):
            station = s[3:].strip()
            continue
        if station and " | RELAX?" in ln:
            guard = ln.split("|", 1)[0].strip().lstrip("| ")
            if guard:
                prev[(station, guard)] = True
    return prev


def _iso_year_week() -> tuple[int, int]:
    y, w, _ = date.today().isocalendar()
    return y, w


def build_digest() -> tuple[str, list[tuple[str, str]]]:
    """Devuelve (markdown, new_flags). new_flags = [(station, guard), ...]
    para candidatos que NO estaban en la semana previa."""
    yr, wk = _iso_year_week()
    prev_yr, prev_wk = (yr, wk - 1) if wk > 1 else (yr - 1, 52)
    prev_path = REVIEWS_DIR / f"guard_review_{prev_yr}-W{prev_wk:02d}.md"
    prev = _prev_verdicts(prev_path)

    lines: list[str] = []
    lines.append(f"# Guard review {yr}-W{wk:02d}")
    lines.append("")
    lines.append(f"Generado {datetime.now(timezone.utc).isoformat(timespec='minutes')} UTC.")
    lines.append("")
    lines.append("Regla fable (asimétrica): apretar N_sole≥20; relajar N_sole≥40 "
                 "+ ROI_sole>0 + sobrevive trim-2. Sólo `sole` decide relajar.")
    lines.append("")

    new_flags: list[tuple[str, str]] = []
    for sid in STATION_IDS:
        try:
            ev = sig.guard_ev(sid, str(CALIBRATION_DB))
        except Exception as e:
            lines.append(f"## {sid}")
            lines.append(f"ERROR: {e}")
            lines.append("")
            continue
        if not ev:
            continue
        lines.append(f"## {sid}")
        lines.append("")
        lines.append("| guard | sole | shared | flag | reason |")
        lines.append("|---|---|---|---|---|")
        for guard in sorted(ev.keys()):
            row, is_cand = _row_for_guard(guard, ev[guard])
            lines.append(row)
            if is_cand and (sid, guard) not in prev:
                new_flags.append((sid, guard))
        lines.append("")

    if not any(ln.startswith("## ") for ln in lines):
        lines.append("_Sin shadow bets settled esta semana._")

    return "\n".join(lines) + "\n", new_flags


def main() -> int:
    REVIEWS_DIR.mkdir(parents=True, exist_ok=True)
    yr, wk = _iso_year_week()
    out_path = REVIEWS_DIR / f"guard_review_{yr}-W{wk:02d}.md"
    md, new_flags = build_digest()
    out_path.write_text(md)
    print(f"wrote {out_path} ({len(md)} bytes, {len(new_flags)} new flags)")
    if new_flags:
        msg = "Nuevos candidatos a relajar: " + ", ".join(
            f"{s}:{g}" for s, g in new_flags)
        _push_ntfy(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
