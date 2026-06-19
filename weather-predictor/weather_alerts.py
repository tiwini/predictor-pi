"""NWS Active Alerts — early warning for cold fronts, severe thunderstorms,
winter storms, tornado watches, wind advisories etc.

The NWS /alerts/active?point=LAT,LON endpoint returns every active
watch/warning/advisory whose polygon covers the given lat/lon. It's free,
no auth, documented at https://www.weather.gov/documentation/services-web-api.

Dataflow:
  fetch_active(station) → [Alert...]
    filtered by `is_temp_relevant` (skip Coastal Flood, Rip Current etc.)
  check_and_push(station) pushes one ntfy per *new* alert id (dedupe via
  notify._sent_cache so each alert only pings once).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date as _date
from typing import Optional

import requests

try:
    import notify as _notify
except Exception:  # pragma: no cover
    _notify = None

NWS_BASE = "https://api.weather.gov"
UA = "weather-predictor/1 jose.rubio.uhy@gmail.com"

# Event types we explicitly drop: unrelated to daily max-temp prediction
# accuracy. Anything not in this set is considered relevant (we lean towards
# surfacing alerts rather than hiding them).
IRRELEVANT_EVENTS = {
    "Coastal Flood Advisory", "Coastal Flood Warning", "Coastal Flood Watch",
    "Coastal Flood Statement",
    "Rip Current Statement",
    "Beach Hazards Statement",
    "Small Craft Advisory", "Gale Warning", "Storm Warning",
    "Hurricane Force Wind Warning",  # marine
    "High Surf Advisory", "High Surf Warning",
    "Lakeshore Flood Advisory", "Lakeshore Flood Warning",
    "Hydrologic Outlook",
    "Flood Statement", "Flood Advisory", "Flood Warning",
    "River Flood Warning", "River Flood Watch",
    "Special Marine Warning",
    "Low Water Advisory",
    "Air Quality Alert",  # useful info but not max-temp predictive
}


@dataclass
class Alert:
    id: str
    event: str
    severity: str            # Extreme, Severe, Moderate, Minor, Unknown
    urgency: str             # Immediate, Expected, Future, Past, Unknown
    certainty: str           # Observed, Likely, Possible, Unlikely, Unknown
    headline: str
    effective: Optional[str]
    ends: Optional[str]
    area_desc: str
    sender_name: str

    def short_id(self) -> str:
        """Stable short id derived from the opaque urn."""
        return hashlib.sha1(self.id.encode()).hexdigest()[:12]


def is_temp_relevant(event: str) -> bool:
    """True if the alert could plausibly affect the max-temp forecast."""
    return event not in IRRELEVANT_EVENTS


def is_high_priority(alert: Alert) -> bool:
    """Warrants priority=high push (else normal). Covers severe weather that
    changes ensemble assumptions or endangers the area."""
    if alert.severity in ("Extreme", "Severe"):
        return True
    if alert.urgency == "Immediate":
        return True
    hi_events = ("Tornado", "Severe Thunderstorm",
                 "Winter Storm", "Blizzard", "Ice Storm",
                 "Red Flag", "Fire Weather", "Excessive Heat",
                 "Heat Advisory", "Wind Chill",
                 "High Wind")
    return any(p in alert.event for p in hi_events)


def parse_features(features: list) -> list[Alert]:
    out: list[Alert] = []
    for f in features:
        p = f.get("properties", {}) if isinstance(f, dict) else {}
        aid = p.get("id") or ""
        event = p.get("event") or ""
        if not aid or not event:
            continue
        out.append(Alert(
            id=aid, event=event,
            severity=p.get("severity") or "Unknown",
            urgency=p.get("urgency") or "Unknown",
            certainty=p.get("certainty") or "Unknown",
            headline=p.get("headline") or "",
            effective=p.get("effective"),
            ends=p.get("ends") or p.get("expires"),
            area_desc=p.get("areaDesc") or "",
            sender_name=p.get("senderName") or "",
        ))
    return out


def fetch_active(station, timeout: float = 10.0) -> list[Alert]:
    """Return relevant alerts active at the station's lat/lon."""
    url = f"{NWS_BASE}/alerts/active"
    try:
        r = requests.get(url,
                         params={"point": f"{station.lat},{station.lon}"},
                         headers={"User-Agent": UA,
                                  "Accept": "application/geo+json"},
                         timeout=timeout)
        if r.status_code != 200:
            return []
        data = r.json()
    except (requests.RequestException, ValueError):
        return []
    all_alerts = parse_features(data.get("features", []))
    return [a for a in all_alerts if is_temp_relevant(a.event)]


def check_and_push(station, target_date: Optional[_date] = None) -> int:
    """Fetch current alerts, push any not previously seen today. Returns
    number of pushes sent. Uses notify's dedupe state, keyed by short_id."""
    if _notify is None or not _notify.enabled():
        return 0
    if target_date is None:
        target_date = _date.today()
    alerts = fetch_active(station)
    sent = 0
    for a in alerts:
        key = f"{target_date.isoformat()}|{station.id}|ALERT:{a.short_id()}"
        if _notify.already_sent(key):
            continue
        title = f"NWS {a.event} · {station.id}"
        lines = [a.headline] if a.headline else [a.event]
        if a.area_desc:
            lines.append(f"Área: {a.area_desc[:160]}")
        if a.severity and a.severity != "Unknown":
            lines.append(f"Severidad: {a.severity} · urgencia: {a.urgency}")
        body = "\n".join(lines)
        priority = "high" if is_high_priority(a) else "default"
        tag = "warning" if priority == "high" else "cloud"
        if _notify.send(title, body, priority=priority, tags=[tag]):
            _notify.mark_sent(key)
            sent += 1
    return sent
