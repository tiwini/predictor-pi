"""El roster vive en `stations.py` y en ningún otro sitio.

Historia de por qué existe este fichero:

    DEFAULT_CROSS = ["KPHX","KLAX","KLAS","KNYC","KBOS"] era el roster de cuando
    el proyecto tenía 5 estaciones. Sobrevivió al paso a 20 y se quedó
    gobernando CUATRO cosas que parecían completas y cubrían un cuarto del
    sistema:

        _refresh_peak_status_cache      marcadores de pico en las tarjetas
        _record_min_snapshots_curated   38 días de datos de mínima, sólo de 5
        _render_alerts_page             ocultaba 4 avisos de calor extremo
        dashboard.WEATHER_STATIONS      ofrecía KIAH, retirada el 2026-07-25

    Ninguna fallaba ni avisaba: servían un roster equivocado con toda
    naturalidad. Las tres primeras murieron el 2026-08-18, la cuarta el
    2026-08-21.

Se comprueba con **AST, no con grep**: las menciones en comentarios y docstrings
documentan el arreglo y deben poder quedarse; lo que no puede volver es una
referencia real en el código.
"""
import ast
import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from stations import STATION_IDS  # noqa: E402

# Todo fichero que haya tenido, o pueda tener, una lista de estaciones.
FICHEROS = [
    BASE / "predictor_web.py",
    BASE / "analysis_poller.py",
    BASE / "kalshi_fast_poller.py",
    BASE.parent / "dashboard.py",
]

# Ids retirados. Reintroducir cualquiera es un bug con nombre propio.
RETIRADOS = {
    "KLGA": "la estación de NY es KNYC (Central Park) desde el 2026-07-22",
    "KIAH": "Houston liquida con KHOU (Hobby) desde el 2026-07-25",
}


def _arboles():
    for f in FICHEROS:
        if f.exists():
            yield f, ast.parse(f.read_text())


def test_los_ficheros_del_test_existen():
    """Si alguno se renombra, el test dejaría de vigilar en silencio."""
    faltan = [f.name for f in FICHEROS if not f.exists()]
    assert not faltan, f"ficheros vigilados que ya no existen: {faltan}"


def test_ninguna_referencia_a_DEFAULT_CROSS():
    malos = []
    for f, arbol in _arboles():
        for n in ast.walk(arbol):
            if isinstance(n, ast.Name) and n.id == "DEFAULT_CROSS":
                malos.append(f"{f.name}:{n.lineno}")
    assert not malos, f"DEFAULT_CROSS vuelve a usarse en {malos}"


def test_ninguna_coleccion_de_estaciones_que_no_sea_el_roster():
    """Cualquier literal con >=5 ids de estación tiene que ser el roster entero.

    Cubre las dos formas en que reapareció: lista con nombre (`DEFAULT_CROSS`) y
    diccionario id→nombre (`WEATHER_STATIONS`). Un subconjunto es exactamente el
    fallo que se persigue; el roster completo es legítimo (mapas de nombres,
    coordenadas...).
    """
    completo = set(STATION_IDS)
    malos = []
    for f, arbol in _arboles():
        for n in ast.walk(arbol):
            if isinstance(n, (ast.List, ast.Set, ast.Tuple)):
                vals = {e.value for e in n.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)}
            elif isinstance(n, ast.Dict):
                vals = {k.value for k in n.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            else:
                continue
            ids = {v for v in vals
                   if len(v) == 4 and v[0] == "K" and v[1:].isupper()}
            if len(ids) >= 5 and ids != completo:
                malos.append(f"{f.name}:{n.lineno} → {sorted(ids)}")
    assert not malos, "colección de estaciones que no es el roster:\n  " + \
                      "\n  ".join(malos)


@pytest.mark.parametrize("sid,porque", sorted(RETIRADOS.items()))
def test_ningun_id_retirado_en_el_codigo(sid, porque):
    """En constantes del código, no en comentarios: los comentarios explican
    por qué se retiró y tienen que poder mencionarlo."""
    malos = []
    for f, arbol in _arboles():
        for n in ast.walk(arbol):
            if isinstance(n, ast.Constant) and n.value == sid:
                malos.append(f"{f.name}:{n.lineno}")
    assert not malos, f"{sid} reaparece en {malos} — {porque}"


def test_el_roster_es_el_esperado():
    assert len(STATION_IDS) == 20
    assert len(set(STATION_IDS)) == 20, "hay ids duplicados"
    for sid in RETIRADOS:
        assert sid not in STATION_IDS
