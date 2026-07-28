# Registro predictivo — 2026-07-28 · KPHX

Escrito **antes** de conocer el settle y commiteado para que el timestamp de git
lo pruebe. Es un test del **bias tracker**, no una recomendación de apuesta.

## Estado al momento de escribir (~09:40 MST)

```
nuestro crudo (con bias)   105.2      -> redondea 105 -> bin "106 or below"
el bias que resta            2.18      (winsorizado; ayer era 3.29)
sin bias                   107.38      -> redondea 107 -> bin "107-108"

pred_iso_med_f             107.7
externos (mediana)         107.2       ext_diff -2.0
mercado Kalshi             106-below 0.35 · 107-108 0.41 · 109-110 0.25

current 84°F · ens_p50 +21.0 sobre current · ventana de pico sin abrir
max_obs 100.9 pero de las 00:51 local (nocturno, no informa)
regime transition · spread 7.6°F · CONVECTIVO · difficulty 45 sin razones
```

## PREDICCIÓN

**El settle de KPHX será ≥107°F**, con `107-108` como bin más probable.

Es decir: la predicción **sin** bias acierta y la que el sistema publica (105.2)
se queda corta.

## Por qué, y por qué NO estoy seguro

A favor: tres estimaciones independientes convergen en ~107 — los externos
(107.2), el bin favorito del mercado (107-108 a 0.41) y nuestra propia
calibrada (107.7). Sólo el crudo con bias se queda en 105.2.

Y sería el **segundo día consecutivo** con el mismo patrón: ayer el sin-bias
acertó con 0.35°F de error y el con-bias falló por 2.94.

En contra, y pesa: el backtest histórico dice que el bias **ayuda** a KPHX —
80% de aciertos y −0.80°F de error mediano sobre N=15
([[backtest_bias_ayuda_2026_07_28]]). Ayer generalicé desde un caso y estaba
equivocado en agregado. Dos días seguidos pueden ser dos excepciones.

## FALSACIÓN

```
settle >= 107   -> el sin-bias acierta; segundo contraejemplo consecutivo
settle <= 106   -> el bias tenía razón y el patrón de ayer era casualidad
settle >= 109   -> ambos se quedan cortos; el problema no es el bias
```

Con dos días medidos seguiría sin haber base para tocar el bias (el backtest
tiene N=15 a su favor), pero sí para justificar un pre-registro específico de
KPHX con N mayor.

## Verificación

```bash
ssh popeye@100.83.162.24
cd ~/predictor-pi/weather-predictor && ./venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from datetime import date
import nws_cli
print(nws_cli.fetch_max_min_for('KPHX', date(2026,7,28)))"
```
