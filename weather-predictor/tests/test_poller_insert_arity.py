"""El INSERT de station_snapshots tiene ~53 columnas escritas a mano y una
lista de `?` igual de larga en otra línea. Añadir una columna y olvidar el
placeholder pasa los tests unitarios y sólo revienta en producción, con el
poller muerto en el primer ciclo:

    sqlite3.OperationalError: 52 values for 53 columns

Ocurrió el 2026-07-27 al añadir `pred_iso_med_f`. Este test compara las tres
listas que tienen que cuadrar: columnas, placeholders y valores.
"""
import ast
import inspect
import re

import analysis_poller


def _insert_stmt(sql_start: str) -> str:
    src = inspect.getsource(analysis_poller)
    i = src.index(sql_start)
    return src[i:src.index('"""', i + len(sql_start))]


def test_station_snapshots_insert_arity_matches():
    stmt = _insert_stmt("INSERT INTO station_snapshots")
    cols_part = stmt[stmt.index("(") + 1:stmt.index("VALUES")]
    cols_part = cols_part[:cols_part.rindex(")")]
    n_cols = len([c for c in cols_part.replace("\n", " ").split(",") if c.strip()])
    n_placeholders = stmt[stmt.index("VALUES"):].count("?")
    assert n_cols == n_placeholders, (
        f"{n_cols} columnas vs {n_placeholders} placeholders en el INSERT de "
        f"station_snapshots — falta o sobra un '?'")


def test_kalshi_snapshots_insert_arity_matches():
    stmt = _insert_stmt("INSERT INTO kalshi_snapshots")
    cols_part = stmt[stmt.index("(") + 1:stmt.index("VALUES")]
    cols_part = cols_part[:cols_part.rindex(")")]
    n_cols = len([c for c in cols_part.replace("\n", " ").split(",") if c.strip()])
    n_placeholders = stmt[stmt.index("VALUES"):].count("?")
    assert n_cols == n_placeholders


def test_signals_dict_covers_every_signal_column():
    """`_compute_signals` devuelve un dict del que el INSERT lee por clave; si
    una columna nueva no está en el dict inicial, el KeyError sólo aparece
    cuando esa rama corre."""
    src = inspect.getsource(analysis_poller._compute_signals)
    declared = set(re.findall(r'"(\w+)":\s*None', src))
    used = set(re.findall(r'sig\[["\'](\w+)["\']\]',
                          inspect.getsource(analysis_poller.poll_one)))
    missing = used - declared
    assert not missing, f"claves leídas del dict pero nunca inicializadas: {missing}"

def _execute_call(sql_start: str) -> tuple[int, int | None]:
    """(placeholders, valores) de la llamada `c.execute` cuyo SQL empieza así.

    Por AST y no por texto: la tupla de valores lleva expresiones con comas
    dentro (f-strings, llamadas) y contarlas a mano se equivoca.
    """
    tree = ast.parse(inspect.getsource(analysis_poller))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) != 2:
            continue
        sql = node.args[0]
        if not (isinstance(sql, ast.Constant) and isinstance(sql.value, str)):
            continue
        if sql_start not in sql.value:
            continue
        vals = node.args[1]
        n_vals = len(vals.elts) if isinstance(vals, ast.Tuple) else None
        return sql.value.count("?"), n_vals
    raise AssertionError(f"no encuentro el c.execute de {sql_start}")


def test_station_snapshots_valores_cuadran_con_placeholders():
    """Los dos tests de arriba comparan columnas con placeholders y NO con la
    tupla de valores, que es la tercera lista que el docstring de este fichero
    promete. Olvidar un valor al añadir una columna pasa esos tests y mata al
    poller en el primer ciclo con "N values for N+1 columns" — el incidente del
    2026-07-27, que es el motivo de este fichero."""
    n_ph, n_vals = _execute_call("INSERT INTO station_snapshots")
    assert n_vals == n_ph, (
        f"{n_vals} valores vs {n_ph} placeholders en el INSERT de "
        f"station_snapshots")


def test_kalshi_snapshots_valores_cuadran_con_placeholders():
    n_ph, n_vals = _execute_call("INSERT INTO kalshi_snapshots")
    assert n_vals == n_ph, (
        f"{n_vals} valores vs {n_ph} placeholders en el INSERT de "
        f"kalshi_snapshots")
