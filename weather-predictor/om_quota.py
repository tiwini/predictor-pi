"""Tracker de cuota diaria Open-Meteo.

Free tier: 10k calls/día (~10k UTC midnight reset). Tras pegar el límite
el 2026-06-22 con el TTL global de 30 min, queremos saber con anticipación
si nos volvemos a acercar — antes de que /comparison y /ladder caigan.

Contador persistente en JSON (one-shot por día), push ntfy al cruzar 80%
y 95% del límite. Cada fetch real (no cache hit) incrementa el contador.
"""
from __future__ import annotations

import json
import threading
from datetime import date
from pathlib import Path

QUOTA_FILE = Path(__file__).parent / "om_quota.json"
DAILY_LIMIT = 10_000
WARN_THRESHOLDS = (0.80, 0.95)  # 8k y 9.5k

_lock = threading.Lock()


def _load() -> dict:
    try:
        return json.loads(QUOTA_FILE.read_text())
    except Exception:
        return {}


def _save(d: dict) -> None:
    QUOTA_FILE.write_text(json.dumps(d))


def _today() -> str:
    return date.today().isoformat()


def count_call(endpoint: str = "om") -> int:
    """Incrementar contador y devolver el total del día. Llamar después de
    cada request a un endpoint Open-Meteo (cuenten o no como exitosas: la
    cuota free se aplica a todas las requests, incluyendo 429s)."""
    with _lock:
        d = _load()
        today = _today()
        if d.get("date") != today:
            d = {"date": today, "calls": 0, "by_endpoint": {}, "alerted": []}
        d["calls"] += 1
        d.setdefault("by_endpoint", {})
        d["by_endpoint"][endpoint] = d["by_endpoint"].get(endpoint, 0) + 1
        _maybe_alert(d)
        _save(d)
        return d["calls"]


def today_count() -> dict:
    """Devuelve {'date', 'calls', 'by_endpoint'} del día actual, vacío si
    el archivo es de otro día."""
    with _lock:
        d = _load()
        if d.get("date") != _today():
            return {"date": _today(), "calls": 0, "by_endpoint": {}}
        return d


def _maybe_alert(d: dict) -> None:
    calls = d["calls"]
    alerted = set(d.get("alerted") or [])
    for thr in WARN_THRESHOLDS:
        key = f"{int(thr * 100)}pct"
        if key in alerted:
            continue
        if calls >= int(DAILY_LIMIT * thr):
            alerted.add(key)
            d["alerted"] = sorted(alerted)
            try:
                import notify
                pct = int(thr * 100)
                priority = "high" if thr >= 0.95 else "default"
                notify.send(
                    title=f"Open-Meteo cuota {pct}%",
                    message=(f"{calls}/{DAILY_LIMIT} calls hoy "
                             f"({pct}% del límite free).\n"
                             f"Por endpoint: {d.get('by_endpoint', {})}"),
                    priority=priority,
                    tags=["warning"],
                )
            except Exception:
                pass
