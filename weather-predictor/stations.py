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

DOCTRINA HOUSTON (2026-07-25): el mercado KXHIGHTHOU liquida con el CLI de
**Houston/Hobby (KHOU, loc "HOU")**, no con Bush Intercontinental (KIAH).
Verificado contra el settle real de Kalshi en dos días: 07-24 Hobby 94 /
Bush 95 → Kalshi resolvió "93° to 94°"; 07-25 Hobby 92 / Bush 95 → mercado
en "92° or below" a 88¢. Hobby corre 1-3°F más fresco en verano (brisa de
la bahía), así que el sesgo no es un offset corregible. Auditar cada
estación nueva contra el `result` de Kalshi de un día ya liquidado —
19/20 estaban bien, ésta no.
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
    # RECALIBRADAS 2026-08-18 en 8 estaciones (KNYC, KDEN, KSAT, KDCA, KDFW,
    # KPHL, KSEA, KATL). `investigacion/backtest_peak_hours.py`, pre-registrado.
    #
    # Cerraban antes de que ocurriera el pico: KSEA dejaba fuera el 73% de los
    # días y KDFW el 57%, siempre por el mismo lado. No es cosmético — la
    # ventana gobierna el techo físico (`physical_gate` vía 1 da el día por
    # cerrado y fija techo max_obs+1.0), el polling adaptativo (pasa de 3 a 10
    # min), `cap_by_floor` (sólo protege con la ventana ABIERTA) y la hora de
    # referencia del corrector de nivel (peak_lo − 2). En los cuatro el error
    # empujaba a dar el día por terminado antes de tiempo.
    #
    # Dos fuentes independientes tuvieron que coincidir: el archivo horario de
    # Open-Meteo (que no depende de nuestro poller) y nuestros METAR
    # descartando días con <80% de cobertura. Donde sólo una señalaba
    # descalibre —KLAS, KBOS, KMIA, KMDW, KHOU, KSFO, KMSY, KOKC— NO se tocó.
    #
    # ⚠ Sólo se ENSANCHA, nunca se estrecha. El criterio pre-registrado
    # proponía (16,19) para KSAT y KDFW, que habría movido la apertura de las
    # 14 a las 16: el fallo demostrado es unidireccional, y estrechar el lado
    # temprano abre un punto ciego que la evidencia no pide (y recorta
    # `cap_by_floor`, que sólo actúa con la ventana abierta).
    #
    # ⚠ Datos de julio-agosto: son ventanas de VERANO. Revisar en otoño.
    lon: float          # longitud °E (negativo = W); usado para ordenar E→W


STATIONS: list[StationConfig] = [
    StationConfig("KPHX", "KXHIGHTPHX",  "PHX", 14, 17, -112.02),
    StationConfig("KLAX", "KXHIGHLAX",   "LAX", 12, 15, -118.41),
    StationConfig("KLAS", "KXHIGHTLV",   "LAS", 14, 17, -115.15),
    StationConfig("KNYC", "KXHIGHNY",    "NYC", 13, 18,  -73.97),
    StationConfig("KBOS", "KXHIGHTBOS",  "BOS", 12, 17,  -71.01),
    StationConfig("KMIA", "KXHIGHMIA",   "MIA", 14, 17,  -80.29),
    StationConfig("KMDW", "KXHIGHCHI",   "MDW", 14, 17,  -87.75),
    StationConfig("KHOU", "KXHIGHTHOU",  "HOU", 14, 17,  -95.28),
    StationConfig("KSFO", "KXHIGHTSFO",  "SFO", 12, 15, -122.38),
    StationConfig("KAUS", "KXHIGHAUS",   "AUS", 14, 17,  -97.67),
    StationConfig("KDEN", "KXHIGHDEN",   "DEN", 13, 18, -104.67),
    StationConfig("KSAT", "KXHIGHTSATX", "SAT", 14, 19,  -98.47),
    StationConfig("KDCA", "KXHIGHTDC",   "DCA", 13, 18,  -77.04),
    StationConfig("KDFW", "KXHIGHTDAL",  "DFW", 14, 19,  -97.04),
    StationConfig("KPHL", "KXHIGHPHIL",  "PHL", 13, 18,  -75.24),
    StationConfig("KSEA", "KXHIGHTSEA",  "SEA", 14, 18, -122.31),
    StationConfig("KATL", "KXHIGHTATL",  "ATL", 13, 18,  -84.43),
    StationConfig("KMSY", "KXHIGHTNOLA", "MSY", 14, 17,  -90.26),
    StationConfig("KOKC", "KXHIGHTOKC",  "OKC", 14, 17,  -97.60),
    StationConfig("KMSP", "KXHIGHTMIN",  "MSP", 14, 17,  -93.22),
]


STATION_IDS: list[str] = [s.id for s in STATIONS]
PEAK_HOURS: dict[str, tuple[int, int]] = {s.id: (s.peak_lo, s.peak_hi) for s in STATIONS}
STATION_TO_SERIES: dict[str, str] = {s.id: s.kalshi_series for s in STATIONS}
STATION_TO_LOCATION: dict[str, str] = {s.id: s.nws_cli_loc for s in STATIONS}

# IANA timezone por estación (DST-aware vía zoneinfo). Vivía en streaks.py;
# movido aquí 2026-07-26 porque nws_cli también la necesita — el CLI hay que
# leerlo sabiendo cuándo terminó el día local de la estación, si no se aceptan
# reports parciales del día en curso.
STATION_TZ: dict[str, str] = {
    "KPHX": "America/Phoenix",
    "KLAX": "America/Los_Angeles",
    "KLAS": "America/Los_Angeles",
    "KNYC": "America/New_York",
    "KBOS": "America/New_York",
    "KMIA": "America/New_York",
    "KDCA": "America/New_York",
    "KPHL": "America/New_York",
    "KATL": "America/New_York",
    "KMDW": "America/Chicago",
    "KHOU": "America/Chicago",
    "KAUS": "America/Chicago",
    "KSAT": "America/Chicago",
    "KDFW": "America/Chicago",
    "KMSY": "America/Chicago",
    "KOKC": "America/Chicago",
    "KMSP": "America/Chicago",
    "KDEN": "America/Denver",
    "KSFO": "America/Los_Angeles",
    "KSEA": "America/Los_Angeles",
}
STATION_TO_LON: dict[str, float] = {s.id: s.lon for s in STATIONS}

# Hora local del PRIMER CLI de la tarde — el primero que reporta el día en curso
# con el max acumulado a fidelidad ASOS 1-min.
#
# CORREGIDA 2026-07-27. La medida original tomaba el ÚLTIMO CLI del mismo día, y
# varios WFO emiten dos o tres: KATL saca uno a las 16:40 y otro a las 20:35, así
# que la tabla decía 20.6 y el poller se perdía **3.9 horas** de piso duro. Igual
# KMSP (3.3h), KDFW (3.0h), KSAT y KAUS (1.2h), KDEN (1.1h), KOKC (1.0h).
#
# Tomar el primero es estrictamente mejor porque el CLI se consume como PISO y
# sólo sube: si el siguiente trae más, el piso sube; nunca baja. Verificado que
# el primero NUNCA sobreestima al final — 23 iguales, 9 por debajo, **0 por
# encima** sobre las 7 estaciones afectadas.
#
# Medida original 2026-07-26
# sobre ~7 días por estación (probe `investigacion/cli_intraday_probe.py`): la
# hora es estable dentro de ±0.3h, y ese report ya trae el max FINAL del día en
# 93 de 102 días-estación (91%). Los 9 desacuerdos son **todos negativos**
# (-1/-2°F): el parcial nunca sobreestima, así que sirve como piso duro.
#
# Cada WFO emite además un CLI matinal (~06:30 local) con el max hasta esa hora,
# que es el que envenenó el settle de KDEN el 07-25 (ver nws_cli). Acá no hace
# daño porque el valor sólo entra vía max(), pero la ventana de fetch arranca
# medio hora antes de ESTA hora para no gastar requests en el matinal.
CLI_LATE_HOUR: dict[str, float] = {
    "KOKC": 16.2, "KHOU": 16.4, "KMIA": 16.4, "KDEN": 16.5, "KDFW": 16.5,
    "KMSP": 16.5, "KMDW": 16.6, "KNYC": 16.6, "KAUS": 16.6, "KSAT": 16.6,
    "KATL": 16.7, "KMSY": 16.8, "KPHX": 17.4, "KDCA": 17.5, "KBOS": 17.5,
    "KLAS": 17.5, "KSFO": 17.6, "KPHL": 17.6, "KSEA": 18.3, "KLAX": 18.6,
}
