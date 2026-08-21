"""`/?station=SID` tiene que llevar a ESA estación (2026-08-21).

Llevaba desde siempre a la estación ACTIVA fuera cual fuera: pedías KMDW y te
salía KPHX, con el número grande de KPHX y ninguna señal de que no era lo
pedido. El dashboard enlaza así, o sea que cada clic desde el panel podía mentir.
"""
import predictor_web


def test_la_ruta_lee_el_parametro():
    """Prueba estructural: `index` mira request.args, no sólo state.station."""
    import inspect
    src = inspect.getsource(predictor_web.index)
    assert 'request.args.get("station")' in src, (
        "si esto desaparece, ?station= vuelve a mentir en silencio")


def test_hay_una_sola_implementacion_del_cambio():
    """Las dos vías (POST del selector y GET del enlace) comparten código.

    Si divergieran, arreglar una dejaría la otra rota — que es exactamente
    cómo nació este bug.
    """
    import inspect
    assert callable(predictor_web._cambiar_estacion)
    post = inspect.getsource(predictor_web.api_station)
    get = inspect.getsource(predictor_web.index)
    assert "_cambiar_estacion" in post and "_cambiar_estacion" in get


def test_estacion_desconocida_no_cae_a_la_activa():
    """El fallo silencioso es peor que el 404: hay que responder que no existe."""
    import inspect
    src = inspect.getsource(predictor_web.index)
    assert "404" in src and "SUPPORTED_STATIONS" in src


def test_set_station_limpia_el_snapshot():
    """La garantía de la que depende todo lo anterior: al cambiar no puede
    quedar el snapshot de la estación previa bajo el nombre de la nueva."""
    import inspect
    import predictor
    src = inspect.getsource(predictor.State.set_station)
    assert "last_snapshot = None" in src
