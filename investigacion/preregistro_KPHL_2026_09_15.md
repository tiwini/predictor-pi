# Pre-registro — KPHL, 2026-09-15, la llamada de las 13h local

Escrito el **2026-09-14 a las 17:10 AST**, el día antes. Sustituye a la hora
elegida ayer: las 15h resultaron estar dentro del territorio del piso, y la
enmienda de `preregistro_KPHL_2026_09_14.md` explica por qué.

```
captura   ./venv/bin/python3 ../investigacion/preregistro.py KPHL --dia 2026-09-15 --hora 13
cierre    ./venv/bin/python3 ../investigacion/preregistro.py KPHL --dia 2026-09-15 --hora 13 --cerrar
```

El cierre entra ~07:00 AST del 2026-09-16. **Mañana hay que cerrar además el
registro del 09-14**, que quedó NULO por piso y banda colapsada:
`preregistro.py KPHL --dia 2026-09-14 --hora 15 --cerrar`.

## Por qué las 13h y no las 15h

La ventana en que el modelo habla es **pre-piso**, no pre-CLI. En septiembre,
cuántos días de 13 la predicción de KPHL **era** el piso:

```
12h  0/13     14h  2/13     16h  4/13
13h  0/13     15h  1/13     17h  7/13
```

A las 13h no manda ni un día. A las 17h manda en más de la mitad — y ése era el
territorio de la tabla de agosto, que por eso medía en parte el clamp.

**El 09-14 a las 13h la llamada habría sido válida y además discriminante**:
banda 1.6°F, holgura 0.8°F al borde, nosotros `76°-77°` (0.61) contra el
`78°-79°` (0.76) del mercado. Se anota como evidencia de diseño —que a esa hora
las dos condiciones se cumplen y el desacuerdo todavía existe— **y no como
resultado**: cuando se escribe esto el máximo observado ya va por 78.1°F, o sea
el desenlace se conoce a medias. Puntuarlo sería elegir después de ver.

## La llamada que se puntúa

Idéntica a la de ayer: **nuestro bin favorito a las 13h local contra el favorito
de Kalshi del mismo ciclo**, ambos por regla. Secundarias: |err| en °F y si la
banda contiene el settle.

**La estación y la hora quedan fijadas aquí.** Si mañana la captura sale NULA,
se anota NULA y se repite otro día: no se busca otra estación ni otra hora
después de ver el día, que es exactamente como se fabrica un falso positivo.

## Invalidaciones — ahora dentro del script, no al pie

`preregistro.py` marca NULA la captura si:

```
snapshot > 10 min                      ya estaba
today_max_cli con valor                ya estaba (el CLI es piso duro)
sin observacion nueva en 90 min        ya estaba (sobre current_obs_ts)
sin bins de Kalshi                     ya estaba
la prediccion ES el piso (±0.06°F)     NUEVA — max_obs, o max_5min − 0.9
banda p10-p90 < 0.05°F                 NUEVA — no hay distribucion que puntuar
```

El margen del piso se **importa** de `predictor.CURRENT_FLOOR_MARGIN_F` en vez
de copiarlo; si ese import se rompe, la captura sale NULA avisando de que el
piso no se ha comprobado, en vez de pasar con un 0.9 escrito a mano que podría
haber dejado de ser verdad.

Verificado en las dos direcciones antes de confiar en él: el 09-14 a las 15h
dispara las dos guardas nuevas, y a las 13h no dispara ninguna.

Aparte, **aviso que no invalida**: si la predicción queda a menos de 0.3°F del
borde del bin, la captura lo dice. Un acierto ahí puede ser redondeo —el settle
es entero y la frontera cae en `hi+0.5`— y conviene leer el resultado con eso
delante en vez de descartarlo.

## Lo que sigue sin poder decidir

**N=1.** No mueve un threshold ni reabre una decisión. Lo que prueba es el
instrumento, y ahora también sus guardas: que se niega a puntuar un día en que
la predicción no es suya. Ayer eso hubo que verlo a mano; mañana lo dice la
captura.
