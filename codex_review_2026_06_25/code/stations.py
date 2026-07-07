"""Single source of truth para las 20 estaciones curadas Kalshi.

Antes esto vivía duplicado en:
  - predictor.py            (PEAK_HOURS)
  - kalshi.py               (STATION_TO_SERIES)
  - nws_cli.py              (STATION_TO_LOCATION — override KLGA→NYC)
  - predictor_web.py        (SUPPORTED_STATIONS)
  - analysis_poller.py      (STATIONS)

Cualquier estación nueva se agrega aquí (una sola línea) y los 5 archivos
la heredan vía import. Re-exportamos las vistas con los mismos nombres
que tenían los originales para minimizar diff en callers.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StationConfig:
    id: str             # NWS METAR id, e.g. "KPHX"
    kalshi_series: str  # Kalshi series ticker, e.g. "KXHIGHTPHX"
    nws_cli_loc: str    # NWS CLI product location code (KLGA→"NYC" Central Park)
    peak_lo: int        # local hour, peak window start (inclusive)
    peak_hi: int        # local hour, peak window end (exclusive)


STATIONS: list[StationConfig] = [
    StationConfig("KPHX", "KXHIGHTPHX",  "PHX", 14, 17),
    StationConfig("KLAX", "KXHIGHLAX",   "LAX", 12, 15),
    StationConfig("KLAS", "KXHIGHTLV",   "LAS", 14, 17),
    StationConfig("KLGA", "KXHIGHNY",    "NYC", 13, 16),
    StationConfig("KBOS", "KXHIGHTBOS",  "BOS", 13, 16),
    StationConfig("KMIA", "KXHIGHMIA",   "MIA", 14, 17),
    StationConfig("KMDW", "KXHIGHCHI",   "MDW", 14, 17),
    StationConfig("KIAH", "KXHIGHTHOU",  "IAH", 14, 17),
    StationConfig("KSFO", "KXHIGHTSFO",  "SFO", 12, 15),
    StationConfig("KAUS", "KXHIGHAUS",   "AUS", 14, 17),
    StationConfig("KDEN", "KXHIGHDEN",   "DEN", 13, 16),
    StationConfig("KSAT", "KXHIGHTSATX", "SAT", 14, 17),
    StationConfig("KDCA", "KXHIGHTDC",   "DCA", 13, 16),
    StationConfig("KDFW", "KXHIGHTDAL",  "DFW", 14, 17),
    StationConfig("KPHL", "KXHIGHPHIL",  "PHL", 13, 16),
    StationConfig("KSEA", "KXHIGHTSEA",  "SEA", 14, 17),
    StationConfig("KATL", "KXHIGHTATL",  "ATL", 14, 17),
    StationConfig("KMSY", "KXHIGHTNOLA", "MSY", 14, 17),
    StationConfig("KOKC", "KXHIGHTOKC",  "OKC", 14, 17),
    StationConfig("KMSP", "KXHIGHTMIN",  "MSP", 14, 17),
]


STATION_IDS: list[str] = [s.id for s in STATIONS]
PEAK_HOURS: dict[str, tuple[int, int]] = {s.id: (s.peak_lo, s.peak_hi) for s in STATIONS}
STATION_TO_SERIES: dict[str, str] = {s.id: s.kalshi_series for s in STATIONS}
STATION_TO_LOCATION: dict[str, str] = {s.id: s.nws_cli_loc for s in STATIONS}
