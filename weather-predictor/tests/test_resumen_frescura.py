"""La sección de frescura de la home.

Lo que se protege aquí es el EMPATE: los METAR salen todos a las :53, así que en
un momento normal casi todas las estaciones comparten edad (medido el 08-21: 18
de 20 dentro de 5 min). Nombrar "las tres más frescas" sería un corte arbitrario
de ese empate, así que la sección reporta CUÁNTAS comparten el mínimo.
"""
import predictor_web


def _card(sid, edad):
    return {"sid": sid, "obs_edad": edad}


def test_empate_se_reporta_como_cuenta_no_como_top3():
    cards = [_card(f"K{i:02d}", 30.0) for i in range(18)]
    cards += [_card("KNYC", 79.0), _card("KDEN", 77.0)]
    r = predictor_web._resumen_frescura(cards)
    assert r["n_en_minimo"] == 18, "las 18 empatadas tienen que contarse todas"
    assert len(r["frescas"]) <= 6, "pero sólo se nombran unas pocas"
    assert r["min"] == 30.0


def test_atrasadas_son_las_que_pasan_el_umbral():
    cards = [_card("KBOS", 30.0), _card("KNYC", 79.0), _card("KDEN", 77.0)]
    r = predictor_web._resumen_frescura(cards)
    assert [a["sid"] for a in r["atrasadas"]] == ["KNYC", "KDEN"], "peor primero"
    assert r["n_atrasadas"] == 2


def test_ninguna_atrasada_cuando_todas_van_al_dia():
    r = predictor_web._resumen_frescura([_card("KBOS", 30.0), _card("KPHL", 31.0)])
    assert r["n_atrasadas"] == 0
    assert r["n_en_minimo"] == 1, "31 no empata con 30"


def test_cards_sin_edad_no_rompen():
    assert predictor_web._resumen_frescura([{"sid": "KBOS"}]) == {}
    assert predictor_web._resumen_frescura([]) == {}


def test_mediana_ignora_los_outliers():
    cards = [_card(f"K{i:02d}", 30.0) for i in range(18)]
    cards += [_card("KNYC", 200.0), _card("KDEN", 300.0)]
    r = predictor_web._resumen_frescura(cards)
    assert r["mediana"] == 30.0, "dos muertas no deben ensuciar la referencia"
