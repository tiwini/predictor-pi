"""Los procesos de larga duración se plantan en su propio directorio.

Va en archivo propio porque no es de ningún módulo: es una propiedad de CÓMO
se arranca la pila, y cubre a los dos que corren 24/7.

El caso que lo motiva: el 2026-10-12 alguien arrancó a mano desde `~` el
`analysis_poller` y el `predictor_web`, y los dos se quedaron seis días con
cwd=/home/popeye. **No falló nada** —`DB_PATH`, `CALIBRATION_DB`, `LOG_DIR` y
las rutas del web salen todas de `Path(__file__)`— y por eso nadie lo vio.

Lo que se cierra es el de después. `start_all.sh` y `proceso_watchdog` hacen
`cd` antes de lanzar, así que esos dos procesos eran los únicos del sistema
con el cwd en otro sitio: el día que aparezca una ruta relativa, el mismo
código escribiría en dos lugares distintos según quién lo hubiera arrancado.
Y en `~` hay un `calibration.db` de 0 bytes esperando exactamente eso — la
trampa del `~/weather-predictor` legacy, que ya se ha cobrado lecturas.

Un `os.chdir` en el entrypoint lo vuelve imposible venga de donde venga el
arranque, en vez de depender de que quien lance se acuerde del `cd`.
"""
import ast
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
ENTRYPOINTS = ["analysis_poller.py", "predictor_web.py"]


@pytest.mark.parametrize("nombre", ENTRYPOINTS)
def test_el_entrypoint_fija_su_cwd(nombre):
    """Se mira el AST y no el texto: un comentario que mencione `os.chdir` no
    debe dar el test por bueno."""
    arbol = ast.parse((BASE / nombre).read_text(encoding="utf-8"))
    bloques = [n for n in arbol.body
               if isinstance(n, ast.If) and "__main__" in ast.dump(n.test)]
    assert bloques, f"{nombre} no tiene bloque __main__"
    chdirs = [n for n in ast.walk(bloques[0])
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)
              and n.func.attr == "chdir"]
    assert chdirs, f"{nombre} no fija su cwd — ver el docstring del módulo"


@pytest.mark.parametrize("nombre", ENTRYPOINTS)
def test_el_chdir_va_antes_de_arrancar(nombre):
    """Después de `main()` no serviría de nada: el web no vuelve de ahí."""
    arbol = ast.parse((BASE / nombre).read_text(encoding="utf-8"))
    bloque = next(n for n in arbol.body
                  if isinstance(n, ast.If) and "__main__" in ast.dump(n.test))
    linea_chdir = min(n.lineno for n in ast.walk(bloque)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr == "chdir")
    linea_main = min(n.lineno for n in ast.walk(bloque)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Name) and n.func.id == "main")
    assert linea_chdir < linea_main
