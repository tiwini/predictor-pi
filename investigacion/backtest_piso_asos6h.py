#!/usr/bin/env python3
"""¿Debe el grupo ASOS de 6h entrar en el piso de observación?

CONTEXTO
--------
KMIA el 2026-08-19 a las 14:22 EDT:

    grupo ASOS 6h    96.08°F   (METAR de 13:53, ventana 07:53→13:53)
    max_obs (feed)   95.00°F
    piso efectivo    95.00°F   ← ignora el ASOS
    ens_med          95.00°F   ← la predicción ES el piso

O sea: el ASOS de 1 minuto —**la misma fuente con la que el NWS liquida**— decía
que el día ya había tocado 96.08, y predecíamos 95.0. Error garantizado de 1.1°F
como mínimo.

`build_snapshot` construye el piso con `max(max_obs, CLI, actual − 0.9)`
(líneas ~1081-1103). `today_max_asos_6h` se calcula (línea ~1037) y se guarda en
el Snapshot, pero **nunca entra en el piso**.

POR QUÉ LA EXCLUSIÓN ES DEFENDIBLE, Y POR QUÉ ES DEMASIADO GRUESA
-----------------------------------------------------------------
El grupo de 6h se acepta hoy si su ventana **intersecta** el día local. Un METAR
de las 02:53 tiene ventana `20:53 de ayer → 02:53 de hoy`: su máximo puede ser
el de **ayer por la tarde**. Meterlo en el piso a ciegas importaría el pico del
día anterior — error peor y en dirección contraria.

Pero cuando la ventana cae **entera dentro del día local** (como el caso de
KMIA: 07:53→13:53), es información dura y válida sobre el día en curso, y se
está tirando.

⚠ SITUACIÓN DE LOS DATOS — leer antes de creerse cualquier resultado
`today_max_asos_6h` **NO está persistido** en `station_snapshots`. Es la tercera
vez que aparece este patrón, tras `current_temp_stable_min` y `current_obs_ts`:
el Snapshot calcula bastante más de lo que guarda. Así que el histórico hay que
**reconstruirlo** desde METARs crudos del archivo de Iowa Mesonet, con el mismo
`parse_metar_6h_max_c` que usa producción — no una reimplementación.

=============================== PRE-REGISTRO ================================
Escrito y commiteado ANTES de descargar ni un METAR.

DOS VARIANTES, y comparar las dos es el punto
  A) INGENUA   el ASOS entra siempre que su ventana intersecte hoy
               (el criterio con el que hoy se ACEPTA la lectura para mostrarla)
  B) PROPUESTA el ASOS entra sólo si su ventana cae ENTERA dentro del día local
               (`window_start >= today_start_local`)

  Si (A) produce violaciones y (B) no, queda demostrado que **la guarda es lo
  que lo hace seguro**, no el ASOS en sí. Si las dos son seguras, la guarda
  sobra y hay que decirlo. Si (B) también viola, se rechaza entero.

MÉTRICA PRIMARIA — SEGURIDAD, y manda sobre todo lo demás
  Una violación es que el piso afirme más de lo que el día dio:
  `floor > settle + 0.5` (mismo redondeo que el resto del sistema; el settle
  del NWS es entero).

  Se cuentan las violaciones del piso ACTUAL y las de cada variante sobre los
  MISMOS station-days. El listón es el que ya se le exigió a `current` cuando se
  añadió al piso: **el riesgo añadido debe ser cero o indistinguible de cero**.

  RECHAZAR si la variante añade violaciones sobre el piso actual.

MÉTRICA SECUNDARIA — BENEFICIO
  |error| mediano de `max(ens_med, floor)` contra el settle, con y sin ASOS.
  Y el tamaño de la oportunidad: en cuántos station-days `asos_6h > max_obs`
  con ventana limpia, y cuánto suben.

CRITERIO DE DECISIÓN sobre la variante (B)
  ADOPTAR  si violaciones_B <= violaciones_actual  Y  N >= 200 station-days
           Y el |error| mediano no empeora
  RECHAZAR si violaciones_B > violaciones_actual
  ESPERAR  si N < 200

  No se pide que el error MEJORE mucho: el piso es una guarda de corrección, no
  una fuente de skill. Que no empeore y no añada riesgo ya justifica cerrar un
  agujero donde hoy predecimos por debajo de lo observado.

LO QUE ESTE BACKTEST NO RESPONDE
  - Si el grupo de 6h es fiable en estaciones que no sean ASOS automáticas.
    El roster son aeropuertos grandes, así que se asume que sí; si alguna diera
    violaciones sistemáticas, se excluiría ESA y no la feature.
  - El efecto sobre bins y `zero_impossible_bins`. Un piso más alto mata más
    bins; eso es correcto por construcción pero cambia la distribución, y
    medirlo pide su propia corrida.

INSTRUMENTACIÓN QUE SE HACE PASE LO QUE PASE
  Persistir `today_max_asos_6h` y `today_max_asos_6h_ts` en `station_snapshots`.
  Cuesta dos columnas y ya está calculado. Sin eso, la próxima pregunta sobre
  este dato vuelve a empezar por reconstruir el histórico.
=============================================================================

Uso:  ./venv/bin/python3 ../investigacion/backtest_piso_asos6h.py [dias]
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path("/home/popeye/predictor-pi/weather-predictor")
sys.path.insert(0, str(BASE))


def main() -> int:
    print(__doc__.split("Uso:")[0])
    print("PENDIENTE DE IMPLEMENTAR — este fichero es el pre-registro.")
    print("Siguiente paso: descargar METARs crudos de Iowa Mesonet y")
    print("reconstruir asos_6h con predictor.parse_metar_6h_max_c.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
