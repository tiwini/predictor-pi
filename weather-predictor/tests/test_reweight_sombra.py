"""La rama en sombra del reweight (2026-09-10, pre-registrada en DECISIONES).

Dos cosas que vigilar, y la segunda es la que se olvida:

1. Que `_rho_entre_horas` mide lo que dice — si las horas ordenan igual a los
   miembros, ρ̄→1 y el efecto de diseño se come las horas repetidas.
2. Que la rama en sombra atraviesa **las mismas cinco transformaciones** que la
   publicada. Si alguien añade una sexta y olvida la gemela, las dos ramas
   dejan de ser comparables y el experimento mide otra cosa sin avisar. Es el
   mismo fallo que documentó el proyecto en «la réplica de una regla no es la
   regla desplegada»: allí el barrido de ventanas se quedó sin poder decidir
   porque la réplica difería 0.46°F del despliegue.
"""
import inspect
import random
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import predictor as P  # noqa: E402


def _matched(residuales_por_miembro, horas=None):
    """residuales_por_miembro[m][h] → matched[m] = [(f, o, hora)] con o=0."""
    n_h = len(residuales_por_miembro[0])
    horas = horas or list(range(6, 6 + n_h))
    return [[(r, 0.0, h) for r, h in zip(fila, horas)]
            for fila in residuales_por_miembro]


# ─── qué mide ρ̄ ──────────────────────────────────────────────────────────────

def test_horas_que_ordenan_igual_dan_rho_uno():
    """Cada miembro con un sesgo constante todo el día: las 8 horas dicen
    exactamente lo mismo sobre quién es el mejor miembro. deff = n_h, o sea
    que las ocho horas valen por una."""
    filas = [[b] * 8 for b in range(-15, 16)]      # 31 miembros
    rho, deff = P._rho_entre_horas(_matched(filas), 8)
    assert rho == pytest.approx(1.0, abs=1e-9)
    assert deff == pytest.approx(8.0, abs=1e-9)


def test_horas_independientes_dan_deff_uno():
    """Residuales sin estructura común: cada hora sí aporta evidencia nueva y
    el SSE no está inflado. Aquí el arreglo no debe hacer nada."""
    rnd = random.Random(20260910)
    filas = [[rnd.gauss(0, 2) for _ in range(6)] for _ in range(300)]
    rho, deff = P._rho_entre_horas(_matched(filas), 6)
    assert abs(rho) < 0.10
    assert deff == pytest.approx(1.0, abs=0.6)


def test_muestra_corta_no_devuelve_nada():
    """Con menos de 5 miembros o menos de 3 horas la correlación no se estima:
    mejor None que un número inventado que luego pesa la distribución."""
    assert P._rho_entre_horas(_matched([[1.0] * 8] * 4), 8) == (None, None)
    assert P._rho_entre_horas(_matched([[1.0, 2.0]] * 31), 2) == (None, None)


def test_miembros_con_horas_incompletas_se_descartan():
    """Un miembro al que le falta una hora tiene otra longitud: mezclar sus
    residuales desalinearía las columnas y ρ̄ mediría ruido."""
    filas = [[b] * 8 for b in range(-15, 16)]
    m = _matched(filas)
    m[0] = m[0][:5]                     # a uno le faltan 3 horas
    rho, deff = P._rho_entre_horas(m, 8)
    assert rho == pytest.approx(1.0, abs=1e-9)


def test_deff_nunca_baja_de_uno():
    """Con ρ̄ negativo el 'arreglo' afilaría los pesos en vez de aplanarlos.
    El suelo en 1.0 lo impide: como mucho, no hacer nada."""
    filas = [[b, -b, b, -b, b, -b] for b in range(-15, 16)]
    _, deff = P._rho_entre_horas(_matched(filas), 6)
    assert deff >= 1.0


# ─── que las dos ramas atraviesen lo mismo ──────────────────────────────────

# Desde el 2026-09-10 las ramas viven en un dict y cada transformación se
# aplica con un bucle, así que añadir una rama no obliga a tocar cinco sitios.
GEMELAS = [
    ("_seasonal", "sombras[_k] = [v - _seasonal for v in sombras[_k]]"),
    ("bias_correction_f",
     "sombras[_k] = [v - bias_correction_f for v in sombras[_k]]"),
    ("ext_shift_f", "sombras[_k] = [v + ext_shift_f for v in sombras[_k]]"),
    ("widen_min_spread", "sombras[_k] = widen_min_spread("),
    ("apply_obs_floor", "sombras[_k] = apply_obs_floor("),
]


def _cuerpo_post_remuestreo():
    src = inspect.getsource(P.build_snapshot)
    return src[src.index("N_SAMPLES = 500"):]


@pytest.mark.parametrize("nombre,gemela", GEMELAS)
def test_cada_transformacion_tiene_su_gemela(nombre, gemela):
    cuerpo = _cuerpo_post_remuestreo()
    assert gemela in cuerpo, (
        f"la rama en sombra no aplica «{nombre}»: dejaría de ser comparable "
        "con la publicada y el experimento mediría otra cosa")


# Toda asignación a `daily_maxes` posterior al remuestreo, normalizada. Las tres
# primeras son el propio remuestreo y sus dos fallbacks; las otras cinco son las
# transformaciones que mueven la distribución, y cada una necesita su gemela.
ASIGNACIONES_CONOCIDAS = sorted([
    "daily_maxes = []",
    "daily_maxes = list(raw_maxes)",
    "daily_maxes = list(raw_maxes)",
    "daily_maxes = [v - _seasonal for v in daily_maxes]",
    "daily_maxes = [v - bias_correction_f for v in daily_maxes]",
    "daily_maxes = [v + ext_shift_f for v in daily_maxes]",
    "daily_maxes = widen_min_spread(daily_maxes, SPREAD_MIN_F.get(station.id))",
    "daily_maxes, obs_floor_n, obs_floor_delta_f = apply_obs_floor(",
])


def test_no_hay_una_transformacion_sin_gemela():
    """EL test de esta instrumentación. Enumera lo que le pasa a `daily_maxes`
    después del remuestreo; si aparece algo nuevo, falla diciendo qué es, y
    obliga a decidir si esa transformación necesita gemela.

    Sin esto, una sexta transformación desplazaría sólo la rama publicada y las
    dos series dejarían de ser comparables — en silencio, que es como duele.
    """
    cuerpo = _cuerpo_post_remuestreo()
    vistas = []
    for ln in cuerpo.splitlines():
        s = ln.split("#")[0].strip()
        if "daily_maxes_alt" in s or not s:
            continue
        if re.match(r"daily_maxes(,[^=]*)? = ", s):
            vistas.append(re.sub(r"\s+", " ", s))
    assert sorted(vistas) == ASIGNACIONES_CONOCIDAS, (
        "cambió lo que le pasa a daily_maxes tras el remuestreo.\n"
        f"  nuevas:      {sorted(set(vistas) - set(ASIGNACIONES_CONOCIDAS))}\n"
        f"  desaparecen: {sorted(set(ASIGNACIONES_CONOCIDAS) - set(vistas))}\n"
        "Si mueve la distribución necesita su gemela en la rama en sombra "
        "y su entrada en GEMELAS.")


def test_la_sombra_no_puede_tocar_lo_publicado():
    """`daily_maxes` no puede leer nunca de una rama en sombra."""
    cuerpo = _cuerpo_post_remuestreo()
    for ln in cuerpo.splitlines():
        s = ln.strip()
        if s.startswith("daily_maxes =") or s.startswith("daily_maxes,"):
            assert "sombras" not in s and "_alt" not in s, \
                f"la sombra se filtró a lo publicado: {s}"


def test_el_snapshot_expone_la_sombra():
    campos = {f for f in P.Snapshot.__dataclass_fields__}
    assert {"ensemble_daily_maxes_alt", "ensemble_daily_maxes_banda",
            "ensemble_eff_n_alt", "reweight_rho", "reweight_deff"} <= campos


# ─── rama B: la anchura del deff con el nivel de la publicada ───────────────

def test_recentrar_pone_la_mediana_donde_se_pide():
    out = P._recentrar([10.0, 12.0, 14.0], 100.0)
    s = sorted(out)
    assert s[len(s) // 2] == pytest.approx(100.0)


def test_recentrar_no_toca_la_anchura():
    """EL punto de la rama B: separar nivel de banda. Si el desplazamiento
    cambiara la forma, no estaría separando nada."""
    m = [10.0, 12.0, 13.0, 20.0]
    out = P._recentrar(m, 50.0)
    assert max(out) - min(out) == pytest.approx(max(m) - min(m))
    for a, b in zip(sorted(m), sorted(out)):
        assert (b - a) == pytest.approx(sorted(out)[0] - sorted(m)[0])


def test_recentrar_con_lista_vacia():
    assert P._recentrar([], 10.0) == []


def test_la_rama_banda_se_construye_de_la_deff():
    """La B sale de la A recentrada, no de un cálculo aparte: si se calcularan
    por separado podrían divergir sin que nada avisara."""
    cuerpo = _cuerpo_post_remuestreo()
    assert 'sombras["banda"] = _recentrar(_mx' in cuerpo
