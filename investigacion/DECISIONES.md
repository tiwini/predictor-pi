# Registro de decisiones

Libro append-only: **toda** señal, regla o guarda que se evaluó, con su criterio
pre-registrado, el resultado medido y el veredicto. Se anota tanto lo adoptado
como lo rechazado — el registro de lo que NO funcionó es la mitad que se pierde
y la que evita repetir el mismo backtest en tres meses.

Reglas de este archivo:

1. Una fila **antes** de correr nada, con el criterio ya fijado (estado `⏳`).
2. Se cierra con el número medido, nunca con una impresión.
3. Lo rechazado **no se borra**. Si algo se reabre, se añade fila nueva
   apuntando a la vieja; la vieja se queda como estaba.
4. Si una decisión se invalida después (el ledger roto de julio invalidó varias),
   se marca `⚠ INVALIDADA` con la razón, sin reescribir el veredicto original.

Las filas anteriores al 2026-08-21 están reconstruidas de las memorias y los
commits del repo; llevan la fuente entre corchetes. Desde el 2026-08-21 se
escriben en el momento.

---

## Piso de observación

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-26 | CLI parcial de la tarde como piso | mejora del \|err\| sin romper el settle | +1.0°F, N=486 | ✅ ADOPTADO [[cli_first_intradia]] |
| 2026-07-26 | Clamp de la predicción bajo `max_obs` | 174/176 casos = error garantizado | violaciones a 0 | ✅ ADOPTADO [[clamp_pred_bajo_max_obs]] |
| 2026-07-27 | `current − 0.9` en el piso | riesgo añadido cero | mismas 1055/65502 violaciones; \|err\| 1.400→1.283; 1231 violaciones a 0 | ✅ ADOPTADO [[backtest_piso_current]] |
| 2026-07-27 | Fallback de 5 min para `max_obs` | no romper el piso | cierra 94% del gap pero rompe el piso el 29% de los días | 🔴 RECHAZADO [[backtest_fallback_5min]] |
| 2026-08-02 | Propagar el piso a la distribución por bin | bins imposibles a cero | criterio correcto es `floor > hi+0.5` | ✅ ADOPTADO [[bugs_piso_no_propagado]] |
| 2026-08-14 | Guarda de techo físico | vetar bins que el día ya no alcanza | la vía p90 casi nunca veta | ✅ ADOPTADO [[physical_gate_implementado]] |
| 2026-08-20 | Grupo ASOS de 6h en el piso, con guarda de ventana | violaciones ≤ actual, N≥200 | N=605, riesgo cero **con** guarda; sin ella 35 violaciones vs 4 | ✅ ADOPTADO variante B [[backtest_piso_asos6h]] |
| 2026-08-22 | ASOS 1-min de Mesonet para la ventana ciega de KDEN | latencia ≤30 min **y** cierra ≥50% del hueco a las 15h **y** cero violaciones | Latencia **29.6 h** (igual pidiendo 48h o 96h ⇒ es el archivo). Tres sondeos en vivo: cero filas. El aporte sería 92-104% —el dato es exactamente el que hace falta— pero llega día y medio tarde | 🔴 RECHAZADO. Predicción estructural CONFIRMADA. La ventana ciega de KDEN queda abierta [[asos1min_kden]] |
| 2026-08-21 | ¿Tapa el ASOS 6h el gap de KDEN y KNYC? | el grupo limpio llega en o antes del cierre de PEAK_HOURS en ≥50% de días **y** cierra ≥50% del gap | KNYC cierra 82-92% desde las 14h ✅. KDEN cierra **0%** entre 14-16h y sólo 101% a las 18h. Mecanismo: mismas horas UTC, distinto huso — KNYC recibe a las 13:53 local, KDEN a las 17:53 | ⚠ El criterio dio ✅ a las dos y **para KDEN era falso**: su métrica de aporte comparaba magnitudes distintas. KNYC ✅ · KDEN 🔴 ciego 12-18h [[asos6h_kden_knyc]] |
| 2026-08-21 | Máximo **corrido** del feed de 5 min en el piso | violaciones añadidas == 0; concentradas en 1-2 estaciones ⇒ excluir esa; ≥3 ⇒ rechazar; N≥200 | N=420 station-days. Añadidas: día UTC +2847, día LOCAL +121 y **todas de KMSP** (1 día, +0.20°F). Sube el piso en el 20.0% de snapshots, mediana +0.90°F, p90 +2.16°F; corrige 375 predicciones ya refutadas por el termómetro | ✅ ADOPTADO con **KMSP excluida** [[piso_max5min]] |

## Corrección de nivel y sesgo

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-28 | El bias tracker en conjunto | ¿aporta? | sí, N=204 — pero el 82% del aporte es del path regime | ✅ parcial [[backtest_bias_ayuda]] |
| 2026-08-05 | Corrector de nivel por mediana causal en KLAX+KSFO | backtest propio por estación | sustituye al EWMA; KDCA queda fuera a propósito | ✅ ADOPTADO [[corrector_nivel_klax]] |
| 2026-08-14 | Jubilar el EWMA | su fuente sólo cubre la estación activa del web | 49.7% = azar | 🔴 JUBILADO [[bias_ewma_muerto]] |
| 2026-08-15 | Corrector de nivel en KNYC | backtest propio | \|err\| 3.21→1.72°F; 16/16 días sobre-prediciendo; bin 2/16→10/16 a las 14h | ✅ ADOPTADO [[backtest_corrector_knyc]] |
| 2026-08-11 | `pred_iso_med_f` como predicción principal | umbral +2pp | +1.96pp | 🔴 RECHAZADO, bajo el umbral [[revision_2026_08_11]] |

## Señales evaluadas y descartadas

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-24 | Regla v3 (bin encima del modo) | ROI con N suficiente | ROI +0.3%, N=126 | 🔴 NO VALIDADA [[rule_v3_bin_encima_modo]] |
| 2026-07-24 | Lag de `our_p` contra Kalshi | ¿lideramos? | era un tick de muestreo; el mercado lidera 2:1, p≈0.004 | 🔴 REFUTADO [[lag_our_p_vs_kalshi_refutado]] |
| 2026-07-27 | `difficulty` como gate | ¿predice el error? | N=505: ninguna componente correlaciona | 🔴 REVOCADO [[feedback_triple_convergence_fails_regime_roto]] |
| 2026-07-27 | Los edges como señal estructural | Brier contra Kalshi | Kalshi nos gana 7/9 (0.078 vs 0.143) | 🔴 NO ESTRUCTURALES [[edges_no_estructurales_brier]] |
| 2026-07-27 | mid-YES | ROI, N≥100 y control propio | ROI −7.5% y el control era espejo aritmético | 🔴 INSUFICIENTE [[backtest_mid_yes]] |
| 2026-08-01 | Nubosidad y radiación | rho dentro de estación | N=584, rho=+0.015 — el GFS ya las incorpora | 🔴 DESCARTADAS [[sky_nubosidad_radiacion_descartado]] |
| 2026-08-01 | Advección | rho dentro de estación | N=540; KSFO/KLAX es offset por estación | 🔴 DESCARTADA [[adveccion_descartada]] |
| 2026-08-01 | Humedad del suelo | signo esperado por Bowen | N=428, el signo va CONTRA Bowen | 🔴 DESCARTADA [[suelo_descartado]] |
| 2026-08-03 | Subir el techo del calibrador sobre 0.50 | ¿es el calibrador o la señal? | our_p 0.80 acierta 0.35 | 🔴 el techo es correcto [[calibrador_techo_050]] |
| 2026-08-20 | Ganarle al mercado por VELOCIDAD | ¿reaccionamos antes? | el mercado mata bins 99 min antes; 39% vs 24% al señalar ganador; 0 de 21 estaciones ganan | 🔴 CANAL CERRADO [[latencia_vs_kalshi_cerrada]] |

## Señales que sí sobrevivieron

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-26 | Convergencia horaria por estación | hora del tope por estación | 13h KLAX → 17h KPHX/KLAS | ✅ [[convergencia_horaria]] |
| 2026-07-27 | `ext_diff` matinal como predictor del error | monotonía | N=483 monotónico: >+3°F a las 08h ⇒ sobre-predecimos 92% | ✅ [[ext_diff_matinal_predice_error]] |
| 2026-08-10 | Fiabilidad por estación × hora | operable pre-CLI | sólo KSEA (13h) y KPHX (15h); KSFO acierta 6% | ✅ acotado [[fiabilidad_estaciones]] |

## Roster y configuración

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-22 | KLGA → KNYC | la estación que liquida Kalshi | Central Park | ✅ APLICADO, nunca reintroducir [[knyc_rename]] |
| 2026-07-25 | KIAH → KHOU para el settle | auditar contra el `result` de Kalshi | Houston liquida con Hobby | ✅ APLICADO [[bug_houston_kalshi_settlea_hobby]] |
| 2026-07-21 | Dividir el roster | 2 gates de reactivación escritos | — | ⏸ EN REPOSO [[roster_split_decision]] |
| 2026-08-18 | `PEAK_HOURS` recalibradas | sólo ENSANCHAR, y dos fuentes deben coincidir | 8 estaciones; KSEA de 73% a 7% de días fuera | ✅ APLICADO [[bug_peak_hours_descalibradas]] |
| 2026-08-18 | Matar `DEFAULT_CROSS` | gobernaba alertas y badges sobre 5 de 20 sin avisar | — | ✅ ELIMINADO [[roster_fantasma]] |

## P&L y ledger

| Fecha | Decisión | Criterio pre-registrado | Medido | Veredicto |
|---|---|---|---|---|
| 2026-07-06 | El ROI +53% del ledger | auditoría | artefacto; 5 fixes P0-P2 + safe mode | ⚠ INVALIDÓ lo anterior [[weather_ledger_broken]] |
| 2026-07-10 | Tail-negation bets | validado sobre el ledger… roto | — | ⚠ INVALIDADA; no readmitir sin N≥100 post-fix [[feedback_tail_negation_bets]] |
| 2026-08-18 | El ROI +49% de `/bets` | recorte al ledger post-fix | 94% de la muestra era pre-fix; real −10.4% con N=38 | 🔴 ARTEFACTO [[bugs_bets_ledger]] |

---

## Notas de método que salieron de estas corridas

**La latencia se mide antes que la calidad.** Dos corridas seguidas (ASOS 6h en
KDEN, ASOS 1-min en KDEN) murieron por *cuándo* llega el dato, no por lo bueno
que es. El 1-min cierra el 92-104% de la ventana ciega y aun así es inservible:
llega 29.6 h tarde. Para cualquier fuente nueva, la primera pregunta es a qué
hora está disponible, y sólo si pasa esa se mira si sirve.


**Un criterio pre-registrado puede estar bien escrito y medir lo que no es.** El
del ASOS en KDEN/KNYC (2026-08-21) dio ✅ a las dos estaciones; en KDEN era falso.
La métrica de "aporte" medía cuánto sube el piso a mediodía —por el grupo de la
mañana— y lo comparaba contra el gap del máximo del día, que lo pone el pico de
la tarde. Dos magnitudes que no se corresponden, y el número salía bonito.

Lo que lo salvó fue haber escrito **una predicción estructural falsable** además
del criterio: decía que el grupo llegaría tarde para el pico. El criterio la dio
por refutada; la medición alineada mostró que era CORRECTA para KDEN. Cuando la
predicción y el criterio se contradicen, hay que ir a mirar por qué antes de
quedarse con el veredicto.


**El criterio se aplica a las violaciones AÑADIDAS, no a las totales.** En la
corrida del 2026-08-21 la primera implementación contaba totales y reportaba 4
estaciones afectadas → RECHAZAR. El diagnóstico por día mostró que KBOS, KPHX y
KDCA tenían **cero** días de exceso: ya violaban con el piso vigente y no
añadían nada. Las añadidas estaban enteras en una estación → EXCLUIR. El
criterio estaba bien escrito; la que estaba mal era su implementación.

**Un backtest de una regla intradía tiene que ser intradía.** El del ASOS de 6h
se analizó por DÍA y fue correcto allí. Aplicar la misma plantilla al máximo
corrido habría sido un sinsentido: a nivel de día `MAX(current_f)` **es** el
máximo corrido, así que las dos variantes salen idénticas por construcción y el
backtest no puede fallar. Un backtest que no puede fallar no mide nada.

**Lo que se despliega tiene que ser lo que se midió.** El máximo corrido se lee
de `station_snapshots.current_f` —la cantidad exacta que evaluó el backtest— y
no de la serie cruda del NWS, que incluye lecturas de 5 min que el poller nunca
vio. Serían dos reglas distintas con el mismo nombre.
