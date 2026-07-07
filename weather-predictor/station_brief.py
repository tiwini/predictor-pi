"""Briefs geo-climáticos fijos por estación.

Cada brief es texto curado con consideraciones locales que afectan la max:
altura, cuerpos de agua, brisas, capa marina, vientos predominantes, etc.
Sirve como contexto siempre visible en /comparison para no olvidar el
"por qué" detrás de los sesgos sistemáticos del modelo.

Si una estación no tiene brief, get() devuelve None y el template oculta
la card. Editar este archivo es la forma de añadir/refinar contexto sin
tocar predictor_web.py.
"""
from __future__ import annotations

# Cada entrada: (header_short, paragraph). header_short se muestra en bold
# arriba del párrafo. paragraph es un solo bloque de texto.

_BRIEFS: dict[str, tuple[str, str]] = {
    "KPHX": (
        "Phoenix · Sonoran Desert · 337 m",
        "Aire muy seco (HR 10-20% en verano), sin cuerpos de agua cercanos "
        "que moderen la max. Pico térmico estable entre 15-16h LST. "
        "Jul-Sep: monsoon introduce nubes vespertinas y polvo (haboobs) — "
        "GFS puede subestimar 1-2°F cuando hay capa de polvo en la mañana "
        "que se quema rápido. Sin brisa marina; vientos calmos al amanecer, "
        "térmicos al mediodía. Ensemble spread típicamente bajo (1-3°F)."
    ),
    "KLAX": (
        "Los Angeles · costa SoCal · 38 m",
        "Marine layer (capa marina del Pacífico) domina jun-sep: mañanas "
        "nubladas, quema entre 10-13h. Si NO quema (flujo onshore NW): "
        "max 60-75°F. Si quema con flujo offshore (Santa Ana): 85-95°F. "
        "GFS regularmente OVERESTIMA en verano por no modelar bien cuándo "
        "rompe la capa — preferir ext_med y NO-sell de bins altos en heat. "
        "Aeropuerto literalmente en la costa: brisa marina suaviza la tarde. "
        "Modo regime: marine_bimodal cuando spread ≥2°F (lowered 2026-06-23)."
    ),
    "KLAS": (
        "Las Vegas · Mojave Desert · 664 m",
        "Desierto puro, HR 5-15% en verano. Elevación moderada: noches "
        "frescas (60-70°F) → max alto (105-115°F). Sin influencia marina, "
        "ensemble spread bajísimo (1-2°F). Brisa catabática leve desde Spring "
        "Mountains al amanecer. Pico 16-17h LST. Heat domes regionales con "
        "KPHX correlacionados — si Phoenix bate récord, Vegas suele acompañar."
    ),
    "KLGA": (
        "Nueva York · LaGuardia (East River) · settle KNYC (Central Park)",
        "OJO: predicción METAR es KLGA (junto al agua, más fresco), pero "
        "Kalshi liquida con Central Park (NYC CLI). Diferencia típica "
        "KNYC > KLGA: 1-3°F en verano (inland más cálido). Heat waves "
        "Jul-Aug con heat domes Atlántico — humedad alta amplifica heat "
        "index pero la max real queda atada al patrón sinóptico. Brisa "
        "marina desde Long Island Sound puede recortar 2-5°F las tardes."
    ),
    "KBOS": (
        "Boston · Logan (Boston Harbor) · costa Atlántica",
        "Aeropuerto literalmente en el agua. Sea breeze front frecuente "
        "primavera/verano cuando inland > water: puede recortar 5-10°F vs "
        "previsión continental. Flujo NW (offshore) = más cálido. Flujo SE "
        "(onshore) = capa marina, max queda 60-75°F en verano. Heat waves "
        "raras (>95°F una o dos veces al año). Diferencia inland (BDL/ORH) "
        "vs Logan puede ser 8-15°F en sea breeze days."
    ),
    "KMIA": (
        "Miami · subtropical · Biscayne Bay (E) + Everglades (W)",
        "Influencia marina constante por la bahía: max raramente >95°F. HR "
        "alta (70-90%) todo el año. Sea breeze diaria casi siempre — recorta "
        "la max sobre la costa pero inland (KTMB, oeste) puede ser 3-5°F más. "
        "Convección vespertina jun-sep: thunderstorms suelen ocurrir DESPUÉS "
        "del pico térmico (16-19h), no recorta la max típicamente. "
        "Sin estación seca real → variabilidad anual baja."
    ),
    "KMDW": (
        "Chicago Midway · continental inland · Lago Michigan al E",
        "Lake breeze efecto fuerte en primavera/verano: viento E/NE → "
        "lake-cooled, max 10-15°F bajo el west side. Viento SW/W → "
        "warm continental (no lake effect, mismas temps que Rockford). "
        "Midway está más inland que ORD pero igual recibe la lake breeze "
        "cuando el front penetra (típicamente 13-16h LST en primavera). "
        "Heat domes Jul-Aug con dewpoints altos (>70°F)."
    ),
    "KIAH": (
        "Houston Intercontinental · Gulf coast · subtropical húmedo",
        "Influencia del Golfo: HR alta (70-90%), sea breeze diaria pero "
        "más débil que MIA por estar inland (~50 km). Heat waves con flujo "
        "S/SW desde el desierto mexicano. Convección vespertina jun-sep. "
        "Max típica 95-100°F en verano, raramente >105°F. Lluvia frecuente "
        "tarde-noche no afecta la max."
    ),
    "KSFO": (
        "San Francisco · costa Pacífica · capa marina dominante",
        "Mismo patrón que KLAX pero MÁS extremo: capa marina del Pacífico "
        "casi permanente jun-ago. Max veraniega 60-75°F típica, raramente "
        ">85°F. Cuando flujo offshore (Diablo winds) rompe el patrón: "
        "heat waves de 90-105°F en 24-48h. GFS muy poco confiable aquí — "
        "ext_med suele ganarle. Spread bajo no significa convicción: "
        "el modelo a veces queda atrapado en el modo equivocado. "
        "MARINE_STATIONS regime detecta esto desde 2026-06-23."
    ),
    "KAUS": (
        "Austin · Texas Hill Country · 149 m",
        "Heat dome regional dominante en verano (jun-sep): max 100-105°F "
        "estables. Influencia del Golfo más débil que Houston por estar "
        "más inland. Vientos S/SE traen humedad del Golfo (dewpoint 70-75°F). "
        "Drought severo amplifica heat (suelo seco → más sensible heat). "
        "Sin marine layer; sin lake effect significativo (Travis es pequeño)."
    ),
    "KDEN": (
        "Denver · High Plains · 1655 m",
        "Elevación alta: max baja por adiabática (95°F en Denver ≈ 105°F a "
        "nivel del mar). Convección vespertina jun-ago muy fuerte por "
        "Rockies inmediatas — thunderstorms 14-18h LST pueden recortar 5-10°F "
        "de la previsión. Spread alto en ensemble cuando hay convección "
        "(SPREAD_TRANSITION_F bajado a 4.5°F el 2026-06-23 por esto). "
        "Downsloping del oeste = heat anómalo (chinook)."
    ),
    "KSAT": (
        "San Antonio · South Texas · 247 m",
        "Heat dome Texas similar a Austin pero más al sur — max 100-105°F "
        "verano estables. Influencia Golfo intermedia (~250 km). "
        "Drought-flash floods: extremos. Vientos S/SE en verano traen "
        "marine layer débil que se evapora temprano."
    ),
    "KDCA": (
        "Washington National · Potomac River · 5 m",
        "Aeropuerto sobre el Potomac: agua suaviza la max 2-4°F vs IAD/BWI "
        "(inland). Heat domes Jul-Aug con humedad alta. Mid-Atlantic clima: "
        "transición entre frente atlántico y continental. Sea breeze leve "
        "desde Chesapeake en algunos días."
    ),
    "KDFW": (
        "Dallas Fort Worth · North Texas · 184 m",
        "Continental, sin influencia marina relevante (Golfo a ~400 km). "
        "Heat dome Texas: max 100-108°F en verano. Spread bajo, convicción "
        "alta. Drought + clear skies = picos por encima de previsión. "
        "Frentes fríos otoño/invierno cruzan rápido."
    ),
    "KPHL": (
        "Philadelphia · 11 m · Delaware River cercano",
        "Mid-Atlantic continental, agua del Delaware suaviza ligeramente. "
        "Heat waves Jul-Aug con flujo SW: 95-100°F. Dewpoints altos verano "
        "(>70°F). Brisa marina desde Delaware Bay débil pero presente en "
        "días calmos."
    ),
    "KSEA": (
        "Seattle-Tacoma · Puget Sound · 132 m",
        "Marine layer del Pacífico vía Strait of Juan de Fuca: verano "
        "fresco (max 70-80°F típica). Heat domes raros pero EXTREMOS cuando "
        "ocurren (record 108°F en 2021). Cuando flujo offshore (E desde "
        "Cascades): 90-100°F. Patrón regime similar a SF pero con menos "
        "frecuencia de marine layer break."
    ),
    "KATL": (
        "Atlanta Hartsfield · Piedmont · 313 m",
        "Elevación media, sin marine influence directa (Golfo a ~400 km, "
        "Atlántico a ~400 km). Heat dome Sureste verano: max 90-100°F. "
        "Urban heat island fuerte (5to aeropuerto más concurrido). "
        "Convección vespertina jun-sep moderada."
    ),
    "KMSY": (
        "New Orleans · Lake Pontchartrain N + Gulf S · 1 m",
        "Subtropical húmedo extremo: HR 75-95% verano. Influencia del lago "
        "y golfo simultánea — sea breeze diaria. Max veraniega 90-95°F "
        "raramente >98°F (saturación de aire). Convección vespertina "
        "casi diaria jun-sep. Huracanes Aug-Oct."
    ),
    "KOKC": (
        "Oklahoma City · Southern Plains · 391 m",
        "Continental seco-húmedo (transición). Heat domes verano: 100-108°F. "
        "Spring: severe weather corridor (tornadoes). Dryline E-W puede "
        "cruzar el aeropuerto y cambiar dewpoint 20°F en horas. Vientos "
        "fuertes S sustained verano."
    ),
    "KMSP": (
        "Minneapolis-St Paul · Upper Midwest · 256 m",
        "Continental fuerte: amplitud anual enorme (-20°F invierno → 95°F+ "
        "verano). Heat waves Jul-Aug con flujo SW desde Plains. Lake breeze "
        "leve desde Mississippi/Minnetonka pero poco impacto. Cold fronts "
        "cruzan rápido en verano — pueden recortar 10-15°F en horas."
    ),
}


def get(station_id: str) -> tuple[str, str] | None:
    """Returns (header, paragraph) tuple for the station, or None if missing."""
    return _BRIEFS.get(station_id.upper())


def has(station_id: str) -> bool:
    return station_id.upper() in _BRIEFS


def all_briefs() -> dict[str, tuple[str, str]]:
    return dict(_BRIEFS)
