"""Un poll no puede aterrizar sobre la estación equivocada (2026-08-21).

`build_snapshot` tarda segundos. Si el usuario cambia de estación mientras
tanto, el resultado de la ANTERIOR se guardaba sobre la nueva. Visto al pasar de
KPHX a KMDW: la home puso "80.00°F ↓ -32.52°F" — que es 112.52 (Phoenix) menos
80.00 (Chicago). El delta no era de ninguna estación: era la resta de dos.
"""
import inspect
import predictor_web


def test_do_poll_descarta_si_cambio_la_estacion():
    src = inspect.getsource(predictor_web.do_poll)
    assert "sid_al_empezar" in src, "hay que fijar de quién es el poll al empezar"
    assert "state.station.id != sid_al_empezar" in src, (
        "y comprobarlo antes de guardar")


def test_el_snapshot_se_construye_de_la_estacion_fijada():
    """No re-leer state.station a mitad: entre la lectura y el build puede
    cambiar, y volveríamos a mezclar."""
    src = inspect.getsource(predictor_web.do_poll)
    assert "build_snapshot(estacion)" in src
    assert "build_snapshot(state.station)" not in src


def test_el_descarte_va_antes_de_tocar_el_estado():
    """El guardián no sirve si se ejecuta después del commit."""
    src = inspect.getsource(predictor_web.do_poll)
    guarda = src.index("state.station.id != sid_al_empezar")
    commit = src.index("state.prev_dist_med")
    assert guarda < commit, "la comprobación tiene que preceder al guardado"
