"""Single source of truth para las 20 estaciones curadas Kalshi.

Antes esto vivía duplicado en:
  - predictor.py            (PEAK_HOURS)
  - kalshi.py               (STATION_TO_SERIES)
  - nws_cli.py              (STATION_TO_LOCATION)
  - predictor_web.py        (SUPPORTED_STATIONS)
  - analysis_poller.py      (STATIONS)

Cualquier estación nueva se agrega aquí (una sola línea) y los 5 archivos
la heredan vía import. Re-exportamos las vistas con los mismos nombres
que tenían los originales para minimizar diff en callers.

DOCTRINA NY (2026-07-22): la estación de Nueva York es **KNYC (Central Park)**,
que es también donde liquida el mercado Kalshi KXHIGHNY. Históricamente el
id fue "KLGA" (LaGuardia) como legacy pero el fetch de obs y el forecast
Open-Meteo *ya* apuntaban a Central Park vía overrides ocultos. Rename
KLGA→KNYC removió esa capa de confusión — el id ahora refleja la fuente
real de datos. Nunca reintroducir "KLGA" como estación.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StationConfig:
    id: str             # NWS METAR id, e.g. "KPHX"
    kalshi_series: str  # Kalshi series ticker, e.g. "KXHIGHTPHX"
    nws_cli_loc: str    # NWS CLI product location code (e.g. "NYC" para KNYC)
    peak_lo: int        # local hour, peak window start (inclusive)
    peak_hi: int        # local hour, peak window end (exclusive)
    lon: float          # longitud °E (negativo = W); usado para ordenar E→W


STATIONS: list[StationConfig] = [
    StationConfig("KPHX", "KXHIGHTPHX",  "PHX", 14, 17, -112.02),
    StationConfig("KLAX", "KXHIGHLAX",   "LAX", 12, 15, -118.41),
    StationConfig("KLAS", "KXHIGHTLV",   "LAS", 14, 17, -115.15),
    StationConfig("KNYC", "KXHIGHNY",    "NYC", 13, 16,  -73.97),
    StationConfig("KBOS", "KXHIGHTBOS",  "BOS", 13, 16,  -71.01),
    StationConfig("KMIA", "KXHIGHMIA",   "MIA", 14, 17,  -80.29),
    StationConfig("KMDW", "KXHIGHCHI",   "MDW", 14, 17,  -87.75),
    StationConfig("KIAH", "KXHIGHTHOU",  "IAH", 14, 17,  -95.34),
    StationConfig("KSFO", "KXHIGHTSFO",  "SFO", 12, 15, -122.38),
    StationConfig("KAUS", "KXHIGHAUS",   "AUS", 14, 17,  -97.67),
    StationConfig("KDEN", "KXHIGHDEN",   "DEN", 13, 16, -104.67),
    StationConfig("KSAT", "KXHIGHTSATX", "SAT", 14, 17,  -98.47),
    StationConfig("KDCA", "KXHIGHTDC",   "DCA", 13, 16,  -77.04),
    StationConfig("KDFW", "KXHIGHTDAL",  "DFW", 14, 17,  -97.04),
    StationConfig("KPHL", "KXHIGHPHIL",  "PHL", 13, 16,  -75.24),
    StationConfig("KSEA", "KXHIGHTSEA",  "SEA", 14, 17, -122.31),
    StationConfig("KATL", "KXHIGHTATL",  "ATL", 14, 17,  -84.43),
    StationConfig("KMSY", "KXHIGHTNOLA", "MSY", 14, 17,  -90.26),
    StationConfig("KOKC", "KXHIGHTOKC",  "OKC", 14, 17,  -97.60),
    StationConfig("KMSP", "KXHIGHTMIN",  "MSP", 14, 17,  -93.22),
]


STATION_IDS: list[str] = [s.id for s in STATIONS]
PEAK_HOURS: dict[str, tuple[int, int]] = {s.id: (s.peak_lo, s.peak_hi) for s in STATIONS}
STATION_TO_SERIES: dict[str, str] = {s.id: s.kalshi_series for s in STATIONS}
STATION_TO_LOCATION: dict[str, str] = {s.id: s.nws_cli_loc for s in STATIONS}
STATION_TO_LON: dict[str, float] = {s.id: s.lon for s in STATIONS}
