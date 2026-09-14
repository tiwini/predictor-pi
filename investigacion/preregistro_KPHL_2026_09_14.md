# Pre-registro — KPHL, 2026-09-14, la llamada de las 15h local

Escrito a las **11:10 AST con el día ABIERTO**, cuatro horas antes de la hora que
se puntúa y seis antes del CLI parcial. El settle entra ~07:00 AST del
2026-09-15. Nada de lo que sigue puede editarse después: el cierre es un
comando, no una relectura.

```
captura   ./venv/bin/python3 ../investigacion/preregistro.py KPHL --hora 15
cierre    ./venv/bin/python3 ../investigacion/preregistro.py KPHL --hora 15 --cerrar
```

La captura no hay que correrla a las 15:00 en punto. El snapshot queda en
`analysis.db` y la regla de selección es fija —el último con `ts ≤ 15:00 local`
dentro de la hora previa—, así que reconstruirla mañana da el mismo número. Lo
que tenía que existir antes es este fichero.

**El cierre está verificado antes de usarlo.** Corrido sobre KPHL del 09-13,
que ya tiene settle: llamada de las 15h `84° to 85°` (our_cal 0.31), mercado
`84° to 85°` (0.66), settle **85.0** por CLI — aciertan los dos, |err| 1.3°F,
banda contiene. Esa corrida se borró después para no dejar en `registros/` un
fichero reconstruido a posteriori con nombre de pre-registro.

## Por qué KPHL y por qué las 15h

Las 20 estaciones están en horario de mañana local a la hora de escribir esto y
**ninguna es legible**: antes de las 12h local el pico no está puesto en ninguna.
De las cinco con ventana pre-CLI conocida, re-medidas **sólo con septiembre**
(N=12-13 días con settle NWS, acierto de bin con los bins reales de Kalshi):

```
             mejor celda PRE-CLI       en AST     ventana pre-CLI
KPHL    15h local  |err| 0.74  10/13    15:00     hasta 17:36    ← elegida
KLAS    16h local  |err| 0.77   7/12    19:00     hasta 20:30
KPHX    16h local  |err| 0.80   6/12    18:00     hasta 20:23
KSEA    13h local  |err| 1.27   5/12    16:00     degradada vs agosto
KAUS    16h local  |err| 1.86   3/12    17:00     rota (bias −1.85)
```

Tres razones que coinciden en KPHL, no una:

1. Mejor celda pre-CLI medida este mes, con 2h 36m de ventana antes de que el
   CLI parcial entre como piso duro. A partir de ahí el acierto mide el CLI y no
   el modelo — el defecto que ya tuvo `dia_vs_mercado.py`.
2. **Mayor holgura al borde de bin del roster hoy: 1.0°F.** El settle es entero,
   así que la frontera real cae en `hi+0.5`. Hoy KSEA, KLAS y KSFO tienen la
   predicción a ≤0.1°F de su frontera: ahí se acierta o se falla por redondeo,
   y la prueba no distinguiría habilidad de suerte.
3. **Discrepamos del mercado de frente**, y se resuelve hoy.

⚠ La tabla de fiabilidad de [[fiabilidad_estaciones_2026_08_10]] decía KSEA 83%
y KPHL 77% a las 17h. Septiembre la mueve: KSEA cae a 42% y KPHL sube y se
**adelanta a las 15h**. La tabla vieja es de 14 días de julio-agosto y está
cruzando el giro de régimen de septiembre. Esta corrida la reemplaza para hoy,
no la deroga: N=13.

## Estado congelado en T-4h (10h local, snapshot 13:53Z)

```
our_pred_f      76.4°F      banda p10-p90  76.1 .. 77.6   (1.5°F)
pred_iso_med_f  76.9°F
externos        74.7°F      ext_diff +0.3
current         73.4°F      obs de hace 25 min · 94 min estable
max_obs         75.9°F      puesto a las 00:54 local (MADRUGADA, no es avance hacia el pico)
piso 5-min      76.0°F      (135 min)
CLI parcial     —           sale ~17:36 local
techo físico    89.7°F      max_obs + p90 de subida restante
difficulty      49          informativo, no correlaciona con el error (N=505)
bias aplicado   0.00        KPHL no está en el corrector de nivel
ventana de pico 13-18h local · pico puesto 100% desde las 16h

nuestro bin     76° to 77°   our_cal 0.571   (our_p crudo 0.764)
bin del mercado 78° to 79°   0.505
```

## La llamada que se puntúa

**Nuestro bin favorito a las 15h local contra el bin favorito de Kalshi a esa
misma hora.** Ambos salen del mismo ciclo, elegidos por regla y no a mano:
el nuestro es el de mayor `our_p_calibrated`, el del mercado el de mayor
`yes_mid`.

Los cuatro resultados posibles, todos informativos:

```
aciertan los dos     el día era fácil; no separa
sólo nosotros        el sistema vio algo que el mercado no
sólo el mercado      el resultado esperado si el sesgo frío de septiembre manda
fallan los dos       el día era raro; mirar qué lo hizo raro
```

Secundarias, que se puntúan igual: **|err| en °F** de `our_pred_f` contra el
settle, y si la **banda p10-p90 contiene el settle** — esta última importa
porque la banda sólo cubre el 54.6% cuando afirma el 80%
([[dispersion_banda_2026_08_28]]), y hoy es de 1.5°F.

## Invalidaciones, escritas antes y comprobadas solas

El script marca la captura NULA si a las 15h local:

- el snapshot tiene **más de 10 min** (hasta 10 es normal, no es bug);
- **`today_max_cli` ya trae valor** — entonces mediría el piso, no el modelo;
- **no hay observación nueva desde hace más de 90 min** (estación callada). Se
  mide sobre `current_obs_ts`, nunca sobre `today_max_obs_ts`: hoy ese marca las
  00:54 porque el máximo fue de madrugada, y leerlo como "callada" fue el primer
  falso positivo que dio este script;
- no hay bins de Kalshi en el ciclo.

El settle se toma **sólo** de `day_outcomes` con `source` en (`cli`, `cf6`). Si
no ha llegado, el cierre imprime PENDIENTE y espera: nunca se sustituye por
`MAX(today_max_obs)`, que difiere del CLI en el 70% de los días
([[feedback_settle_proxy_vs_nws_cli]]).

## Lo que ya sabemos antes de puntuar, y juega en nuestra contra

**KPHL va frío a las 15h en septiembre**, pero menos de lo que parece:

```
09-01 −2.1  09-02 +0.1  09-03 +0.0  09-04 −0.1  09-05 −0.3  09-06 −2.5  09-07 −0.1
09-08 −1.0  09-09 −0.3  09-10 −0.9  09-11 +0.9  09-12 −0.1  09-13 −1.3

negativos 10/13   p=0.092 (signos, dos colas)   mediana −0.30°F   media −0.58°F
```

La media la arrastran dos días (−2.1 y −2.5); la mediana es −0.30. **No pasa el
test de signos al 5%**, así que esto no autoriza a corregir nada —
[[feedback_backtest_before_tuning]]. Se anota porque el mercado está hoy 2°F por
encima de nosotros y esa es exactamente la dirección del sesgo: si el mercado
gana, el candidato a explicación ya está escrito antes de verlo.

**El `ext_diff` de hoy no es señal**: +0.3 pero clampeado, el externo está 1.3°F
por debajo del máximo ya observado. No cuenta como confirmación de nada.

## Lo que esta prueba NO puede decidir

**N=1.** No mueve ningún threshold, no valida ni invalida el modelo, no reabre
ninguna decisión de `DECISIONES.md`. Un solo día no distingue un sistema que
acierta el 77% de uno que acierta el 50%.

Lo que sí prueba, y es para lo que se monta: que **el instrumento entero
funciona de punta a punta** — que el snapshot está fresco a la hora buena, que
los bins de Kalshi llegan, que las invalidaciones disparan cuando deben, que el
CLI cierra el día y que el veredicto sale de un comando y no de una lectura.
Ver [[enfoque_instrumento_2026_08_21]].

---

## ⚠ ENMIENDA escrita a las 16:55 AST, con el día abierto y ANTES del settle

El CLI parcial de KPHL sale ~17:36 local; esto se escribe antes, y se sella en
commit propio para que se vea que no es una relectura posterior.

**La llamada de las 15h no la hizo el modelo: la hizo el piso.**

```
15:00 local (snapshot 18:59Z)
  our_pred_f      77.9°F      banda p10-p90  77.9 .. 77.9   ← 0.0°F de ancho
  today_max_5min  78.8°F      78.8 − 0.9 = 77.9 = la prediccion, exacta
  today_max_obs   75.9°F      (METAR horario, iba 60 min atras)
  current         78.8°F      la prediccion iba 0.9°F por DEBAJO del termometro
```

`CURRENT_FLOOR_MARGIN_F = 0.9` (`predictor.py:440`) es medio escalón de °C —
el feed llega en °C enteros— y está fijado por dos tests. O sea el número es
correcto y el piso hizo exactamente lo que debe. Lo que falla es **esta prueba**:
a las 15h el ensemble no estaba hablando.

**La invalidación que escribí esta mañana estaba incompleta.** Contemplaba el
CLI parcial como vía de piso y no la otra: el piso del feed de 5 min hace lo
mismo, y hoy tomó el control a las **13:49 local**, casi cuatro horas antes que
el CLI. La ventana en que el modelo habla es **pre-piso**, no pre-CLI, y es
mucho más corta de lo que dice el fichero de arriba.

**Y el desacuerdo que motivaba el día se disolvió antes de la hora.** A las 10h
íbamos 76-77 contra el 78-79 del mercado; a las 15h los dos decíamos 78-79
(nosotros 0.811, mercado 0.895). Gane quien gane, no separa a nadie.

### Lo que sí deja, y vale más que el registro

Cuántos días de septiembre la predicción de KPHL **era** el piso, por hora local
(N=13, `max(today_max_obs, today_max_5min − 0.9)`, tolerancia 0.06°F):

```
12h  0/13     14h  2/13     16h  4/13
13h  0/13     15h  1/13     17h  7/13   ← banda colapsada en 5/13
```

A las 17h el piso manda en **más de la mitad de los días**. Así que la tabla de
"acierto por hora" con la que elegí esta mañana —y la de
[[fiabilidad_estaciones_2026_08_10]] que la precede— **mezcla modelo y piso**, y
el acierto que sube con la tarde está en parte comprado con información que no
es del modelo. Es el mismo defecto que ya se documentó en `dia_vs_mercado.py`
(comparar el modelo contra su propio piso), reaparecido en otra métrica.

A las 15h el piso manda 1 de 13 días, así que **la elección de la hora no estaba
contaminada** — pero hoy tocó justo ese día.

### Qué se puntúa mañana, entonces

El cierre se corre igual y el resultado se anota, pero **no cuenta como prueba
del modelo**: a esa hora la predicción era el piso y el bin coincidía con el del
mercado. Cuenta como prueba del instrumento, que es lo que se pedía de una N=1.

Para una prueba del **modelo** hace falta llamar **antes de que el piso tome el
control** —en KPHL de septiembre, a las 13h el piso no manda ni un día de 13— y
exigir que la banda no esté colapsada. Las dos condiciones son medibles en la
captura y deberían ser invalidaciones del script, no notas al pie.
