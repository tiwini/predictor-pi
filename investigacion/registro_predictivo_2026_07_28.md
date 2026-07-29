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

---

# Casos 2 y 3 — KDFW y KATL (añadidos a las ~16:05 EDT)

Anotados **después** de que el margen se estrechara respecto a cuando los
sugerí. Los números son los del momento de escribir, no los de hace una hora.

## Caso 2 — KDFW `102-103`

```
15:03 CDT · ↗ subiendo · ventana 2.0h · heatwave p96
base 98.6 (current 98.6, max_obs 98.1 de las 13:53, sin moverse en 70 min)
subida restante mediana a esta hora: +2.2°F  (min -0.4, max +5.1, N=27)

  bin        empírico   mercado   EV/$1
  100-101      55.6%      0.62    -0.064
  102-103      37.0%      0.34    +0.030   <- anotado
```

**PREDICCIÓN: el settle cae en `102-103`.** El empírico lo pone en 37% contra
un mercado de 0.34 — margen fino. Nótese que el empírico ya considera
`100-101` MÁS probable (55.6%); la apuesta es de valor, no de resultado más
probable.

## Caso 3 — KATL `97-98`

```
16:03 EDT · ventana 1.1h · heatwave · CONVECTIVO
base 93.9 (max_obs de las 14:52, sin moverse en 70 min)
subida restante mediana a esta hora: +1.4°F  (min -0.0, max +4.0, N=27)

  bin        empírico   mercado   EV/$1
  93-94        11.1%      0.23    -0.119
  95-96        74.1%      0.67    +0.071
  97-98        14.8%      0.08    +0.068   <- anotado
```

**PREDICCIÓN: el settle cae en `97-98`.** ⚠ Con la salvedad de que **ya no es
mejor que `95-96`**: los EV están empatados (+0.068 vs +0.071). Cuando lo
sugerí, a las 15:22, el empírico daba 37% a `97-98` y 51.9% a `95-96`; en 40
minutos pasó a 14.8% y 74.1%. Se anota por continuidad con lo que dije, no
porque siga siendo la mejor elección.

## ⚠ Las dos comparten el mismo supuesto

Ambas apuestan a que el mercado sobrevalora el bin conservador e infravalora el
siguiente escalón. **No son independientes**: si ese sesgo no existe hoy, fallan
las dos a la vez. Contarlas como dos aciertos o dos fallos sería engañarse; a
efectos de evidencia valen aproximadamente por una.

## FALSACIÓN

```
KDFW settle en 102-103   -> acierto            <=101 o >=104 -> fallo
KATL settle en 97-98     -> acierto            <=96  o >=99  -> fallo
```

Además, el dato que más informa no es el acierto sino **si el empírico batió al
mercado**: comparar |empírico - resultado| contra |mercado - resultado| en los
dos bins de cada estación.

---

# RESULTADO (verificado 2026-07-29 contra el CLI final)

```
KPHX   107.0    KDFW   100.0    KATL   94.0
```

## Caso 1 — KPHX ✅ ACERTADO

Predije **≥107**, con `107-108` como bin más probable. Settle **107.0**.

```
sin bias        107.38   error +0.38°F   -> bin 107-108   ✓
con bias        105.20   error -1.80°F   -> bin "106 or below"   ✗
```

**Segundo día consecutivo** en que la predicción sin bias acierta con menos de
0.5°F y la que publica el sistema falla de bin. Ayer: 113.35 vs settle 113.0.

⚠ **Pero el corrector nuevo habría fallado también.** Su mediana causal para
KPHX era −2.25, o sea corregido = 107.38 + 2.25 = **109.63**, error **+2.63°F**:
peor que no corregir nada. Primer dato en vivo, y va en contra del corrector.

## Casos 2 y 3 — KDFW y KATL ❌ AMBOS FALLADOS

```
KDFW  predije 102-103   settle 100.0 -> 100-101   (mercado 0.62, tenía razón)
KATL  predije  97-98    settle  94.0 ->  93-94    (mercado 0.23; el favorito
                                                   del mercado, 95-96 a 0.67,
                                                   también falló)
```

El supuesto compartido —que el mercado sobrevalora el bin conservador— queda
**refutado**: en KDFW ganó el conservador y en KATL ganó uno **aún más**
conservador. Como estaba anotado, esto cuenta como **una** refutación, no como
dos errores.

## El error de método, confirmado

Las obs de ayer por la tarde ya lo anticipaban:

```
KDFW  14:53 -> 99.0   settle 100.0   (+1.0 en el resto de la tarde)
KATL  14:52 -> 93.9   settle  94.0   (+0.1)
```

Ambas estaban **aplanando**, y mi empírico usaba una mediana de subida restante
que **no condiciona por la pendiente reciente**: mezcla días en pleno ascenso con
días ya estancados. Con la temperatura plana durante una hora, la mediana
histórica seguía prometiendo +1.4 (KATL) y +2.2 (KDFW).

**Corrección concreta para la próxima**: condicionar la subida restante por la
pendiente de la última hora. Si lleva ≥45 min sin subir, usar sólo los días
históricos que también estaban planos a esa hora.
