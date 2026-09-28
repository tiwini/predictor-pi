"""Nadie vuelve a comparar un `ts` ISO contra el `datetime('now')` de SQLite.

El 2026-09-28 se encontró el patrón en siete consultas de `investigacion/f1_radar`:

    WHERE ts > datetime('now', '-21 days')

`datetime()` devuelve `2026-09-07 19:08:13` con ESPACIO, y los `ts` de
`station_snapshots` y `kalshi_snapshots` son ISO con `T`
(`2026-09-07T19:17:23.128665+00:00`). SQLite compara esas columnas como
CADENAS, y `'T'` (0x54) es mayor que `' '` (0x20), así que **entra el día
entero del corte**: 51.205 filas en vez de 49.228 en la ventana de 21 días
(+4,0%), arrancando a las 00:10 en lugar de a las 19:17.

Lo que hace peligroso a este fallo es que **no rompe nada**: la consulta
devuelve filas, el script termina y el número sale. Es de la familia de «la
misma asimetría ha mordido tres veces» — el valor aparece, con la fuente
equivocada. Por eso se vigila con un test y no con cuidado.

Lo correcto es comparar en el mismo formato que el dato:

    WHERE ts > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-21 days')

o construir el corte en Python con `.isoformat()`, que es lo que ya hace todo
el código de producción.

NO es el antipatrón comparar una columna DATE (`YYYY-MM-DD`) contra
`date('now', ...)`, ni envolver los dos lados en `date()`/`datetime()`: ahí los
formatos ya coinciden. El test apunta sólo a la comparación cruda de un `ts`.
"""
import ast
import re
import subprocess
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent.parent

# `ts` (o cualquier columna que acabe en _ts) comparado directamente contra el
# datetime/julianday de SQLite, sin normalizar el formato.
ANTIPATRON = re.compile(
    r"\b\w*ts\s*(?:>|<|>=|<=)\s*(?:datetime|julianday)\s*\(\s*['\"]now['\"]",
    re.I)


def _fuentes():
    salida = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=RAIZ,
        capture_output=True, text=True, check=True).stdout.split()
    return [RAIZ / f for f in salida if "venv/" not in f]


def _lineas_de_documentacion(texto: str) -> set:
    """Líneas ocupadas por docstrings.

    Se excluyen porque un texto que EXPLICA el antipatrón no es el antipatrón:
    las notas que documentan este arreglo lo citan literalmente, y sin esto el
    test se marcaría a sí mismo. La distinción correcta es código contra
    documentación, no una lista de excepciones por fichero.
    """
    try:
        arbol = ast.parse(texto)
    except SyntaxError:
        return set()
    fuera = set()
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, (ast.Module, ast.ClassDef,
                                 ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        cuerpo = getattr(nodo, "body", None)
        if not cuerpo:
            continue
        primero = cuerpo[0]
        if (isinstance(primero, ast.Expr)
                and isinstance(primero.value, ast.Constant)
                and isinstance(primero.value.value, str)):
            fuera.update(range(primero.lineno, primero.end_lineno + 1))
    return fuera


def test_ningun_ts_iso_se_compara_con_el_now_de_sqlite():
    culpables = []
    for f in _fuentes():
        if f.resolve() == Path(__file__).resolve():
            # Este fichero cita el patrón como CÓDIGO, no como prosa: las
            # aserciones de abajo comprueban que la regex lo reconoce, y sin
            # esto el detector se marca a sí mismo. Excluir los docstrings por
            # AST no basta para eso.
            #
            # No salió a la primera: mientras el fichero estuvo sin trackear,
            # `git ls-files` no lo listaba y el test pasaba sin mirarse. Se vio
            # a sí mismo en cuanto entró al índice — pasaba por la razón
            # equivocada, igual que el de `_estado_reweight` con `BASE`.
            continue
        try:
            texto = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        documentacion = _lineas_de_documentacion(texto)
        for n, linea in enumerate(texto.splitlines(), 1):
            if n in documentacion or linea.lstrip().startswith("#"):
                continue
            if ANTIPATRON.search(linea):
                culpables.append(f"{f.relative_to(RAIZ)}:{n}: {linea.strip()}")
    assert not culpables, (
        "comparan un ts ISO contra el now() de SQLite; usa "
        "strftime('%Y-%m-%dT%H:%M:%S', 'now', ...) o el corte desde Python:\n  "
        + "\n  ".join(culpables))


def test_el_detector_reconoce_el_patron_que_se_arregló():
    """Que el test falle cuando debe, no sólo que pase cuando todo va bien."""
    assert ANTIPATRON.search("WHERE ts > datetime('now', '-21 days')")
    assert ANTIPATRON.search('WHERE ts>datetime("now")')
    assert ANTIPATRON.search("AND current_obs_ts < datetime('now', '-2 hours')")


@pytest.mark.parametrize("linea", [
    # columna DATE contra date(): los dos formatos son YYYY-MM-DD
    "WHERE station_id=? AND date >= date('now', '-7 days')",
    # los dos lados normalizados
    "AND date(datetime(ts, ?)) = date('now', ?, '-1 day')",
    # la forma correcta
    "WHERE ts > strftime('%Y-%m-%dT%H:%M:%S', 'now', '-21 days')",
])
def test_el_detector_no_marca_lo_correcto(linea):
    assert not ANTIPATRON.search(linea)
