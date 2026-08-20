"""El piso de observación aplicado a la distribución por bin.

Contexto: `apply_obs_floor` (2026-07-26) clampeó la predicción puntual pero no
la distribución. Medido el 2026-08-02: la isotónica levanta el piso MIN_P de
0.030 a ~0.090 en bins que el día ya dejó atrás, y en KAUS llegó a 0.346 siendo
el bin favorito calibrado.

El caso de redondeo (test_umbral_respeta_medio_grado) es el que importa: el
primer análisis usó `bin_hi < floor` y contó 28 bins imposibles, pero el settle
del NWS es entero y `our_p_for_bin` cubre [lo-0.5, hi+0.5]. Con max_obs 98.06 el
bin "98 or below" sigue vivo. El criterio correcto es `floor > hi + 0.5`.
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from predictor import zero_impossible_bins, obs_floor_from_snapshot  # noqa: E402


def B(lo, hi):
    return SimpleNamespace(bin_lo=lo, bin_hi=hi)


def test_anula_bin_ya_superado_y_conserva_la_masa():
    bins = [B(float("-inf"), 90), B(91, 92), B(93, float("inf"))]
    ps = [0.09, 0.50, 0.10]
    out, n, liberada = zero_impossible_bins(bins, ps, floor=92.0)
    assert n == 1 and out[0] == 0.0
    assert abs(liberada - 0.09) < 1e-9
    # la masa liberada se reparte entre los vivos, sin cambiar el total
    assert abs(sum(out) - sum(ps)) < 1e-9
    # y proporcionalmente: el que tenía 5x del otro sigue teniendo 5x
    assert abs(out[1] / out[2] - ps[1] / ps[2]) < 1e-9


def test_umbral_respeta_medio_grado():
    """El settle es entero: con max_obs 98.06 el bin '98 or below' VIVE.

    Éste es el caso donde el criterio ingenuo `hi < floor` se equivoca — mataría
    un bin que gana si el día no sube más.
    """
    bins = [B(float("-inf"), 98), B(99, 100)]
    ps = [0.35, 0.30]
    out, n, _ = zero_impossible_bins(bins, ps, floor=98.06)
    assert n == 0 and out == ps

    # a partir de floor > 98.5 sí queda fuera de alcance
    out2, n2, _ = zero_impossible_bins(bins, ps, floor=98.6)
    assert n2 == 1 and out2[0] == 0.0


def test_sin_piso_o_sin_imposibles_es_identidad():
    bins = [B(float("-inf"), 90), B(91, 92)]
    ps = [0.2, 0.6]
    assert zero_impossible_bins(bins, ps, None) == (ps, 0, 0.0)
    assert zero_impossible_bins(bins, ps, 80.0) == (ps, 0, 0.0)


def test_tail_superior_nunca_es_imposible():
    bins = [B(120, float("inf"))]
    out, n, _ = zero_impossible_bins(bins, [0.05], floor=200.0)
    assert n == 0 and out == [0.05]


def test_no_revienta_si_todos_son_imposibles():
    """Caso degenerado: sin bins vivos no hay dónde redistribuir."""
    bins = [B(float("-inf"), 70), B(71, 72)]
    out, n, liberada = zero_impossible_bins(bins, [0.4, 0.4], floor=95.0)
    assert n == 2 and out == [0.0, 0.0]
    assert abs(liberada - 0.8) < 1e-9


def test_none_en_ps_no_rompe():
    """Un bin sin probabilidad calculada se deja intacto y no arrastra al resto."""
    bins = [B(float("-inf"), 80), B(96, 97), B(98, 99)]
    out, n, liberada = zero_impossible_bins(bins, [0.1, None, 0.5], floor=95.0)
    assert n == 1 and out[0] == 0.0
    assert out[1] is None                      # se respeta tal cual
    assert abs(liberada - 0.1) < 1e-9
    assert abs(out[2] - 0.6) < 1e-9            # recibe toda la masa liberada


def test_piso_desde_snapshot_toma_el_mayor_de_los_tres():
    s = SimpleNamespace(today_max_obs=88.0, today_max_cli=91.0,
                        current_temp_f=85.0)
    assert obs_floor_from_snapshot(s) == 91.0
    # current manda cuando va por delante, con su margen de medio escalón °C
    s2 = SimpleNamespace(today_max_obs=88.0, today_max_cli=None,
                         current_temp_f=95.0)
    assert abs(obs_floor_from_snapshot(s2) - 94.1) < 1e-9
    # sentinel de "sin observación" no debe colarse como piso
    s3 = SimpleNamespace(today_max_obs=-999.0, today_max_cli=None,
                         current_temp_f=None)
    assert obs_floor_from_snapshot(s3) is None


# ── Regresión 2026-08-17: el web pasa dicts, no objetos ──────────────────
#
# `/comparison` y `/ladder` reimplementan el pipeline (dist → isotónica →
# blend) y no llamaban a este paso final. Al conectarlo apareció el segundo
# fallo, más silencioso: allí los bins son dicts y `getattr(b, "bin_hi")`
# devolvía None, así que TODOS caían en la rama de vivos y la función no
# anulaba nada mientras aparentaba funcionar.
#
# Caso real que lo destapó: KPHX el 2026-08-17 con max_obs 111.9°F mostraba
# ≤107 / 108-109 / 110-111 al 7.7-9.7% en /comparison, cuando el poller los
# tenía en 0.0 para ese mismo instante.

def D(lo, hi):
    """Bin en la forma que usa el web."""
    return {"bin_lo": lo, "bin_hi": hi}


def test_acepta_bins_como_dict():
    bins = [D(float("-inf"), 107), D(108, 109), D(110, 111),
            D(112, 113), D(114, 115), D(116, float("inf"))]
    ps = [0.077, 0.082, 0.097, 0.494, 0.080, 0.077]
    out, n, liberada = zero_impossible_bins(bins, ps, floor=111.9)
    assert n == 3, "los tres bins bajo 111.9 son imposibles"
    assert out[0] == 0.0 and out[1] == 0.0 and out[2] == 0.0
    assert out[3] > 0.494, "la masa liberada se redistribuye a los vivos"
    assert abs(sum(out) - sum(ps)) < 1e-9, "la suma total se conserva"


def test_dict_y_objeto_dan_el_mismo_resultado():
    """Sin esto, la misma cifra saldría distinta en el web y en el poller."""
    lims = [(float("-inf"), 107), (108, 109), (110, 111), (112, 113)]
    ps = [0.077, 0.082, 0.097, 0.494]
    como_obj, n1, m1 = zero_impossible_bins([B(*l) for l in lims], list(ps), 111.9)
    como_dict, n2, m2 = zero_impossible_bins([D(*l) for l in lims], list(ps), 111.9)
    assert como_obj == como_dict and n1 == n2 and m1 == m2


def test_dict_sin_piso_es_identidad():
    bins = [D(float("-inf"), 107), D(108, 109)]
    ps = [0.3, 0.7]
    out, n, _ = zero_impossible_bins(bins, ps, floor=None)
    assert out == ps and n == 0


# ── El grupo ASOS 6h en el piso (2026-08-20) ─────────────────────────────
#
# El ASOS de 1 min es la MISMA fuente con la que liquida el NWS y llega antes
# que el CLI. KMIA el 08-19 lo tenía en 96.08 con nuestro feed en 95.0, y
# predecíamos 95.0.
#
# Sólo entra la variante LIMPIA: ventana de 6h entera dentro del día local. Sin
# esa guarda, un METAR de 05:53Z arrastra la tarde de AYER en husos americanos.
# Backtest pre-registrado sobre 605 station-days: sin guarda el piso violaría el
# settle 35 veces en vez de 4; con ella, exactamente las mismas 4.

def _snap_asos(asos_f, asos_hora_utc, max_obs=95.0):
    """Snapshot mínimo con un ASOS a una hora UTC dada. tz = Miami."""
    from datetime import datetime, timezone as _tz
    from zoneinfo import ZoneInfo
    mia = ZoneInfo("America/New_York")
    ts = datetime(2026, 8, 19, asos_hora_utc, 53, tzinfo=_tz.utc)
    return SimpleNamespace(
        today_max_obs=max_obs, today_max_cli=None, current_temp_f=None,
        today_max_asos_6h=asos_f, today_max_asos_6h_ts=ts,
        station_local=ts.astimezone(mia))


def test_asos_con_ventana_limpia_sube_el_piso():
    """El caso KMIA: METAR de 17:53Z, ventana 11:53→17:53Z = 07:53→13:53 local."""
    f = obs_floor_from_snapshot(_snap_asos(96.08, 17))
    assert f == 96.08, "el ASOS manda cuando su ventana es de hoy"


def test_asos_que_cruza_medianoche_NO_entra():
    """METAR de 05:53Z: ventana 23:53→05:53Z = 19:53 de AYER → 01:53 local."""
    f = obs_floor_from_snapshot(_snap_asos(100.9, 5))
    assert f == 95.0, "no puede importar el pico de ayer"


def test_asos_mas_bajo_no_baja_el_piso():
    """El piso sólo sube: es un max, nunca un reemplazo."""
    assert obs_floor_from_snapshot(_snap_asos(90.0, 17)) == 95.0


def test_sin_asos_el_piso_es_el_de_siempre():
    s = _snap_asos(None, 17)
    s.today_max_asos_6h_ts = None
    assert obs_floor_from_snapshot(s) == 95.0
