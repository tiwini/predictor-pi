"""El INSERT de station_snapshots tiene ~53 columnas escritas a mano y una
lista de `?` igual de larga en otra línea. Añadir una columna y olvidar el
placeholder pasa los tests unitarios y sólo revienta en producción, con el
poller muerto en el primer ciclo:

    sqlite3.OperationalError: 52 values for 53 columns

Ocurrió el 2026-07-27 al añadir `pred_iso_med_f`. Este test compara las tres
listas que tienen que cuadrar: columnas, placeholders y valores.
"""
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
