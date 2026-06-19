"""ntfy.sh push notifications. Opt-in via NTFY_TOPIC env var.

Uso: setea `export NTFY_TOPIC=weather-predictor-xxxxx` (topic único tuyo) y
luego subscríbete en la app ntfy al mismo topic. Free, sin cuenta.
"""
import os
import threading
from datetime import date
from pathlib import Path

import requests

NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh")
TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
UA = "weather-predictor/0.1"

# Dedupe state: set of keys already pushed today. Persisted in a flat file
# so restart doesn't re-spam. Key format: "YYYY-MM-DD|station|ticker".
STATE_FILE = Path(__file__).parent / ".notify_sent.txt"
_lock = threading.Lock()
_sent_cache: set | None = None


def enabled() -> bool:
    return bool(TOPIC)


def _load_cache() -> set:
    global _sent_cache
    if _sent_cache is not None:
        return _sent_cache
    today = date.today().isoformat()
    keep = set()
    if STATE_FILE.exists():
        for ln in STATE_FILE.read_text().splitlines():
            ln = ln.strip()
            if ln and ln.startswith(today + "|"):
                keep.add(ln)
    _sent_cache = keep
    return _sent_cache


def _persist() -> None:
    if _sent_cache is None:
        return
    STATE_FILE.write_text("\n".join(sorted(_sent_cache)) + "\n")


def already_sent(key: str) -> bool:
    with _lock:
        return key in _load_cache()


def mark_sent(key: str) -> None:
    with _lock:
        _load_cache().add(key)
        _persist()


def send(title: str, message: str, priority: str = "default",
         tags: list | None = None) -> bool:
    """Send one push. Returns True on success, False if disabled or failed.

    HTTP/1.1 header values are latin-1; emoji or other non-latin-1 chars in
    the title would crash requests. Tags already handle emoji on ntfy's side
    (the 'warning' tag renders ⚠), so we strip unsafe chars from the title
    rather than failing silently.
    """
    if not enabled():
        return False
    safe_title = title.encode("latin-1", errors="replace").decode("latin-1")
    headers = {
        "User-Agent": UA,
        "Title": safe_title,
        "Priority": priority,
    }
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        r = requests.post(f"{NTFY_URL}/{TOPIC}",
                          data=message.encode("utf-8"),
                          headers=headers, timeout=10)
        return r.ok
    except requests.RequestException:
        return False


def alert_edge(station_id: str, target_date: date, ticker: str,
               label: str, our_p: float, kalshi_p: float) -> None:
    """Alert once per (date, station, ticker) when |edge| is large."""
    key = f"{target_date.isoformat()}|{station_id}|{ticker}"
    if already_sent(key):
        return
    edge = our_p - kalshi_p
    direction = "sobre" if edge > 0 else "bajo"
    sign = "+" if edge > 0 else ""
    title = f"Edge {station_id} · {label}"
    message = (f"Kalshi {direction}-pricea bin {label}.\n"
               f"Nuestro: {our_p*100:.0f}% · Kalshi: {kalshi_p*100:.0f}% · "
               f"edge {sign}{edge*100:.1f}pp")
    tag = "chart_with_upwards_trend" if edge > 0 else "chart_with_downwards_trend"
    if send(title, message, priority="high", tags=[tag, "money_with_wings"]):
        mark_sent(key)


def alert_settled(station_id: str, target_date: date, max_f: float,
                  our_brier: float | None, kalshi_brier: float | None) -> None:
    key = f"{target_date.isoformat()}|{station_id}|SETTLED"
    if already_sent(key):
        return
    title = f"Settled {station_id} · {target_date.isoformat()}"
    lines = [f"Max real: {max_f:.1f}°F"]
    if our_brier is not None and kalshi_brier is not None:
        winner = "nosotros" if our_brier < kalshi_brier else (
            "Kalshi" if kalshi_brier < our_brier else "empate")
        lines.append(f"Brier nuestro: {our_brier:.3f} · Kalshi: "
                     f"{kalshi_brier:.3f} · gana {winner}")
    elif our_brier is not None:
        lines.append(f"Brier: {our_brier:.3f}")
    if send(title, "\n".join(lines), priority="default",
            tags=["white_check_mark"]):
        mark_sent(key)


def alert_regime_break(station_id: str, target_date: date,
                       break_hours: list[int],
                       eff_n: float | None = None,
                       reason: str = "p1-p99") -> None:
    """Alert once per (date, station) when the ensemble looks structurally
    wrong. `reason` controls the wording: 'p1-p99' (≥2 obs fuera del rango),
    'eff_n_low' (reweight colapsado), 'combo' (1 break + eff_n bajo)."""
    key = f"{target_date.isoformat()}|{station_id}|REGIME"
    if already_sent(key):
        return
    title = f"Ruptura de régimen · {station_id}"
    hrs = ",".join(f"{h:02d}h" for h in sorted(set(break_hours))) if break_hours else ""
    if reason == "eff_n_low":
        body = (f"El reweight bayesiano colapsó (eff_n = {eff_n:.1f}/31): "
                "el ensemble no se parece a lo observado.")
    elif reason == "combo":
        body = (f"Obs fuera del rango a las {hrs} y eff_n = {eff_n:.1f}/31 "
                "(reweight muy agresivo). El pronóstico probablemente está roto.")
    else:
        body = (f"La observación cayó fuera del p1-p99 del ensemble en "
                f"{len(break_hours)} hora(s): {hrs}.")
    body += "\nConsidera saltar hoy."
    if send(title, body, priority="high",
            tags=["warning", "chart_with_downwards_trend"]):
        mark_sent(key)
