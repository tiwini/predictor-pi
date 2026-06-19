# Revisión weather-predictor — auditoría de calibración, bias tracker, anclaje externo y bet-ranking

Fecha de análisis: 2026-06-11. Datos: `snapshots_last30_auto.csv` (5.474 filas, 2026-05-12 → 06-10), `day_outcomes_last60.csv` (130 settles), `simulated_bets_last60.csv` (457 settled), código completo del zip más `lectura.py`. Los settles del 06-10 (KPHX 107, KLAS 106, KBOS 90, KMIA 88, KMDW 91, KLGA 82) los añadí a mano desde tu brief porque el CSV termina el 06-09. No pude ejecutar la suite (pytest no está disponible en este sandbox y no hay red), así que todo lo que propongo está verificado con `py_compile` sobre copias parcheadas, pero corre tú `./venv/bin/python3 -m pytest tests/` antes de mergear — los tests `test_bias_tracker.py` y `test_bets_gate.py` van a necesitar ajustes que indico abajo.

Una nota sobre los datos antes de empezar: `day_summary_last60.csv` **no contiene columnas de ext_diff ni de bias aplicado**, aunque el brief dice que sí (las columnas reales son brier/winning_bin/best_edge). Eso significa que no existe historial de la señal externa por día, y por tanto el umbral exacto de la regla "externos mandan" no se puede backtestear con lo que me diste — solo se puede razonar estructuralmente y validar hacia adelante. La recomendación cero de este informe es: empieza a loggear `ext_med`, `ext_spread` y `ext_diff` matinales en `day_summary` hoy mismo. En 3-4 semanas tendrás el dataset que este análisis necesitaba.

---

## 1. Auditoría de calibración

### 1a. Primero, un artefacto que contamina la tabla de snapshots

La curva de fiabilidad sobre las 4.898 filas con outcome da algo absurdo: el bucket p∈(0.8, 1.0] (n=1.702, p media 0.995) acierta solo el **16.9%**. Mirando esas filas, casi todas son snapshots de tarde-noche (21–07 UTC) cuyo threshold modal es un valor continuo tipo `67.458` o `97.306122` — es decir, la máxima METAR ya observada (19.7 °C → 67.46 °F) convertida de Celsius. El sistema afirma "la máxima es ~67.46±0.5 con p≈1" y luego el NWS CLI settlea en 69. Dos causas se combinan: el settle CLI no es la máxima METAR horaria (grupos de 6 horas, redondeo, y en NY directamente **otra estación**: KLGA predice pero KNYC settlea — el caso KLGA 2026-05-30, 174 filas con threshold 67.458 contra settle 69, es exactamente eso), y el bin ±0.5 alrededor de un valor continuo queda desalineado con el entero del settle. Conclusión práctica: las filas vespertinas con p≈1 no miden calibración del modelo, miden el gap METAR↔CLI. Vale la pena snapear el threshold del slot modal a `round()` cuando el pico ya está confirmado, y tratar KLGA/KNYC como riesgo de settle aparte (el delta mediano KLGA observado en esas filas es ~+1.5 °F).

### 1b. Calibración en la ventana que importa (mañana, 6–12h local)

Filtrando a snapshots de 6–12h local (deduplicados por hora; n=192) — la ventana en la que se deciden las bets — la curva queda así:

| p predicha (bin modal) | n | frecuencia observada |
|---|---|---|
| 0.25 | 53 | 0.226 |
| 0.34 | 40 | **0.100** |
| 0.44 | 45 | 0.222 |
| 0.55 | 30 | **0.000** |
| 0.71 | 11 | 0.545 |
| 0.97 | 7 | 0.143 |

El Brier matinal global es **0.223**, peor que el 0.142 de un pronosticador constante que dijera siempre la base rate (17.2%). Es decir: en la mañana, la probabilidad que el modelo asigna al bin modal tiene **skill negativo** — el número p no aporta información sobre la frecuencia real más allá de la base rate, y por encima de p≈0.3 está sistemáticamente inflado en 20-50 puntos. Ver `fig_calibracion.png` (panel izquierdo).

Por estación (Brier matinal / base rate): KPHX 0.164/0.195 (el único decente), KBOS 0.216/0.188, KLAX 0.236/0.273, KMIA 0.261/0.167, KMDW 0.272/0.333, KLGA 0.280/0.056, KLAS 0.285/0.156. KLGA es el peor caso relativo: base rate 5.6% con Brier 0.28 — coherente con el problema KNYC.

Por percentil climatológico (proxy: percentil empírico del settle dentro de la distribución 60d de cada estación — los CSVs no traen percentil verdadero):

| percentil del día | n | p media asignada al modal | acierto real |
|---|---|---|---|
| p25–50 | 37 | 0.38 | 0.49 |
| p50–75 | 76 | 0.45 | 0.09 |
| p75–85 | 27 | 0.35 | 0.04 |
| p85–95 | 26 | **0.44** | **0.00** |
| p95+ | 25 | 0.38 | 0.28 |

En p85–95 — exactamente el régimen de tu pregunta — el modelo asigna 44% al bin modal y acierta **0 de 26**. Y la dirección del fallo es unilateral: el error matinal (real − pred modal) tiene media **+2.4 °F (mediana +2.0)** en días de ola de calor contra +0.8 en días normales (panel derecho de la figura). Por estación en días calientes: KBOS +6.3 (n=9), KLGA +2.2, KPHX +2.1, KLAS +0.8 con mediana +2.0. O sea: sí, estás descalibrado específicamente en el lado caliente durante heatwaves, y no es ruido — es sesgo frío sistemático del posterior, que convierte cada NO de cola alta en una apuesta contra la física.

### 1c. La misma historia desde las bets settled

Sobre las 451 bets liquidadas con settle conocido, comparando la probabilidad que asignamos al lado tomado contra el win rate real:

| dirección | n | p nuestra (media) | win rate | gap | PnL |
|---|---|---|---|---|---|
| hot (apostar caliente) | 26 | 0.81 | 0.77 | +4pp | +218 |
| cold (apostar frío) | 44 | 0.60 | 0.41 | **+19pp** | +182 |
| mid | 381 | 0.71 | 0.52 | +19pp | +2.374 |
| mid en día de calor | 48 | 0.75 | **0.375** | **+37pp** | **−234** |
| cold en día de calor | 6 | 0.47 | 0.17 | +30pp | −49 |

El lado caliente está casi calibrado; el lado frío y los bins medios en heatwave son donde se quema el dinero. El PnL global (+2.750, WR 52.5%) sobrevive porque Kalshi pricea aún peor que nosotros en días normales, no porque nuestras p sean buenas.

### 1d. Hallazgo colateral grave: las auto-bets no pasan por la isotónica

Esto explica parte de la sobreconfianza medida arriba. En `predictor_web.py`, `comparison_view` aplica `isotonic.apply()` y luego `blend_with_external` — pero `_check_edge_alerts` (el camino que llama a `bets.maybe_bet`) toma `our_p` directamente de `kalshi.latest_snapshot`, que guarda el valor **crudo** de `our_p_for_bin` (`kalshi.py` línea ~177), le aplica solo el blend externo y dispara la bet. La capa de calibración isotónica que construiste **nunca toca el camino del dinero**: la ves en la UI, pero las bets (y por tanto toda la tabla `simulated_bets`) se deciden con la probabilidad más sobreconfiada del sistema. El diff 06 lo corrige replicando el mismo orden (isotónica → blend) que ya usa el comparison view.

---

## 2. Diagnóstico del bias tracker

Tu pregunta era si el EWMA con regime-break es demasiado lento para una heatwave que rampa en 2-3 días. La respuesta corta: sí es lento (estructuralmente no puede reaccionar antes del día 4), pero acelerarlo es la dirección equivocada, porque la señal que consume no predice el error de mañana.

**Lo estructural.** El detector exige REGIME_K=4 días consecutivos del mismo signo con |media| ≥ 1.5, y `compute_bias` exige MIN_DAYS=3 de historia. Una rampa de 2-3 días termina antes de que el detector exista. Además el "early pred" del SQL es el primer snapshot tras las 08:00 **UTC** (4am EDT, 1am PDT/PHX) — funciona de casualidad porque tus snapshots matinales empiezan ~11 UTC, pero es frágil.

**Lo empírico, que es peor.** Reconstruí la serie de error diario (pred matinal − settle) por estación y le pasé una réplica exacta de `compute_bias` (EWMA α=0.4, regime-break con `_extreme_bias`). Resultado: aplicar el tracker tal como está codificado **empeora** el MAE en 4 de 7 estaciones — KPHX 1.91→2.16, KBOS 4.20→4.95, KMIA 2.17→3.04, KMDW 1.75→2.22 — y es ~neutro en el resto. La razón está en la autocorrelación lag-1 del error diario sobre días consecutivos: **−0.25 pooled (n=40 pares)**. Tras un día con error ≤ −2 °F, el error medio del día siguiente es solo **−0.73 °F**. El error de ayer no se repite entero hoy; corregirlo 1:1 sobreajusta, y `_extreme_bias` — que toma el **peor** error de los últimos 4 días — sobreajusta el doble. Caso concreto en `fig_bias_lag.png`: KPHX 06-04, el tracker aplica −5.0 °F de corrección un día cuyo error real era 0.0, fabricando un error de +5. Advertencia honesta sobre el tamaño muestral: 40 pares es poco, KLGA da autocorrelación positiva (+0.37), y mi "pred matinal" es el threshold modal (lleva ~0.5 °F de ruido de redondeo). Pero el signo del resultado es robusto a eso: no hay evidencia de que el error diario persista lo suficiente como para justificar correcciones agresivas basadas en ayer.

**Respuesta a tus dos preguntas.** ¿REGIME_K=4 / REGIME_MIN_ABS=1.5 demasiado lentos? Sí, pero no los bajes: con autocorrelación ~0 o negativa, un detector más sensible solo perseguiría ruido más rápido. ¿Añadir un ajuste forward-looking cuando los externos corren ≥+1.5 °F por encima? **Sí, exactamente eso — pero en el predictor, no en el tracker.** La mediana externa es señal del mismo día (lag 0 por construcción); el tracker, por diseño, siempre llega un settle tarde. El rol correcto del tracker es corregir el sesgo estacionario lento (el "+2 de KBOS, −2 de KPHX" de su docstring), y para eso el diff 03 reemplaza `_extreme_bias` por media-del-bloque × 0.5 con tope ±2.5 °F. `test_regime_trigger.py` y `test_bias_tracker.py` asumen el comportamiento worst-case y habrá que actualizar sus aserciones.

---

## 3. Propuesta concreta: la señal externa dentro del posterior

Primero una corrección al brief: la señal externa **ya no es solo sidecar**. `blend_with_external` se aplica en `comparison_view` y en `_check_edge_alerts` antes de `maybe_bet`. Pero opera al nivel de probabilidad-por-bin, con rampa lenta (w = 0.15·(|d|−1.5), o sea que con ext_diff −3.6 solo llega a w≈0.32) y no toca el posterior. Consecuencia: la "Máxima esperada", el bin modal, la climatología, el percentil que alimenta al bias condicional, la pred que scrapea `lectura.py` — todo eso sigue ciego a los externos. El 06-10 el simulador igualmente colocó `KBOS NO [88+]` (our_p 0.59 post-blend, settle 90: pierde) y `KLAS YES [103-104]` (settle 106: pierde). El blend amortiguó y no alcanzó.

**Crítica a tu idea (α=0.5 fijo cuando ext_diff ≥ +1.5 y clim ≥ p85).** Cuatro problemas. (1) Acantilado: ext_diff 1.4 → nada, 1.6 → corrección máxima; en la frontera el sistema oscila día a día. (2) Solo cubre el régimen caliente; el caso simétrico (KLAS cold-side, tu memoria del 05-26) queda fuera. (3) No degrada con el spread externo: si los 6 modelos discrepan 6 °F entre sí, su mediana no merece α=0.5. (4) Doble conteo: si mueves el posterior 50% del gap y *además* dejas el blend por-bin activo con el ext_diff viejo, corriges dos veces. Y un límite que tu propia tabla del 06-10 muestra: la mediana externa también se quedó corta (105.7 vs 107, 104.5 vs 106), así que ningún α ≤ 0.5 habría cerrado el gap completo — el anclaje reduce el sesgo a la mitad, pero la protección de cola (sección 4) es la que evita la pérdida.

**Lo que propongo (diffs 01 y 02, compilados y verificados):** un *mean-shift* del posterior en `build_snapshot`, después de la corrección de bias: `daily_maxes += λ·(ext_med − pred_med)`, con λ de rampa continua, pendiente 0.25/°F sobre el umbral, tope 0.5, umbral 1.5 °F normal que **baja a 1.0 °F con bonus +0.15 cuando clim ≥ p85 y vamos fríos** (tu `feedback_heatwave_externals_win`, codificada), y guardia `spread > 5.4 °F → λ=0` (mismo criterio que `MAX_MODELS_SPREAD_F` de bets). El doble conteo se resuelve solo, sin código extra: `_anchor_context` calcula ext_diff desde la distribución *ya desplazada*, así que el blend por-bin se auto-atenúa; y el bias tracker aprende del threshold de snapshots post-shift, o sea aprende el sesgo *residual*. Sobre el 06-10 esto habría dado: KBOS 86.1 → ~87.9 (λ al tope 0.5), KLAS 102.9 → ~103.5, KPHX 104.3 → ~104.7 (entra solo por el umbral rebajado a 1.0 en régimen caliente). No te salva las tres, pero mueve el modal un bin hacia la verdad y, combinado con la isotónica en el camino de bets (diff 06) y el gate direccional (diff 04), mata las dos NO frías.

```diff
--- weather-predictor/external_models.py
+++ weather-predictor/external_models.py
@@ -203,3 +203,42 @@
 
     _cache_set(key, result)
     return result
+
+
+# ── Anchor en el POSTERIOR (no solo por-bin) ──
+# ext_diff convención: pred_med - ext_med (negativo = vamos fríos).
+POSTERIOR_SHIFT_THRESHOLD_F = 1.5  # |ext_diff| mínimo para empezar a mover
+POSTERIOR_SHIFT_SLOPE = 0.25       # peso por °F de exceso sobre el umbral
+POSTERIOR_SHIFT_CAP = 0.50         # nunca movemos más de la mitad del gap
+POSTERIOR_HEAT_BONUS = 0.15        # extra si clim p>=85 y vamos fríos
+POSTERIOR_MAX_SPREAD_F = 5.4       # externos en desacuerdo => no anclamos
+
+
+def posterior_shift_weight(ext_diff, ext_spread, clim_percentile):
+    """Peso lambda en [0, CAP] para shift = lambda * (ext_med - pred_med).
+
+    Rampa continua (sin acantilado), umbral más bajo en régimen
+    caliente-subestimado (feedback_heatwave_externals_win), y guardia de
+    spread: si los 6 modelos externos discrepan >3°C, su mediana no es
+    ancla fiable.
+    """
+    if ext_diff is None:
+        return 0.0
+    if ext_spread is not None and ext_spread > POSTERIOR_MAX_SPREAD_F:
+        return 0.0
+    heat_under = (clim_percentile is not None and clim_percentile >= 85
+                  and ext_diff < 0)
+    thr = 1.0 if heat_under else POSTERIOR_SHIFT_THRESHOLD_F
+    if abs(ext_diff) < thr:
+        return 0.0
+    w = (abs(ext_diff) - thr) * POSTERIOR_SHIFT_SLOPE
+    if heat_under:
+        w += POSTERIOR_HEAT_BONUS
+    return min(POSTERIOR_SHIFT_CAP, max(0.0, w))
```

```diff
--- weather-predictor/predictor.py
+++ weather-predictor/predictor.py
@@ (en build_snapshot, justo después del bloque de bias y antes de peak_status)
+    # ─── Anclaje externo en el posterior (post-bias) ───
+    # Corrige TODO lo derivado del posterior (pred, bin modal, climatología,
+    # our_p_for_bin) el mismo día, sin el lag de 1+ días del tracker. El
+    # blend por-bin sigue activo pero se auto-atenúa: _anchor_context
+    # recalcula ext_diff sobre la distribución ya desplazada.
+    ext_shift_f = 0.0
+    ext_shift_info = None
+    try:
+        import external_models as _ext
+        mm = _ext.fetch_multi_model_max(station)
+        if mm is not None and mm.median is not None and daily_maxes:
+            _sm = sorted(daily_maxes)
+            _pred_med = _sm[len(_sm) // 2]
+            _ext_diff = _pred_med - mm.median
+            _clim_pct = None
+            if _climate_percentile is not None:
+                try:
+                    _cs = _climate_percentile(station, today, _pred_med)
+                    _clim_pct = _cs.percentile if _cs is not None else None
+                except Exception:
+                    _clim_pct = None
+            _lam = _ext.posterior_shift_weight(_ext_diff, mm.spread, _clim_pct)
+            if _lam > 0.0:
+                ext_shift_f = _lam * (mm.median - _pred_med)
+                daily_maxes = [v + ext_shift_f for v in daily_maxes]
+            ext_shift_info = {
+                "ext_med": mm.median, "ext_spread": mm.spread,
+                "ext_diff_pre": _ext_diff, "clim_pct": _clim_pct,
+                "lambda": _lam, "shift_f": ext_shift_f,
+            }
+    except Exception:
+        ext_shift_info = None
     if prob_rising >= 0.50:
```

Detalles de implementación que te conviene saber: `fetch_multi_model_max` ya cachea 30 min, así que esto no añade latencia al poll; el `try/except` mantiene el TUI funcionando sin red; y si quieres ver el shift en la UI, añade `ext_shift_f: float = 0.0` y `ext_shift_info: dict | None = None` al dataclass `Snapshot` y pásalos en el constructor — el diff completo en `parches_propuestos.diff` no incluye esos dos campos para mantenerlo mínimo. Un efecto de segundo orden a vigilar: el shift entra *antes* del cómputo de climatología del snapshot (bien, queremos el percentil de la pred corregida) pero el `pct_lookup` histórico del bias condicional usa thresholds antiguos pre-shift; durante las primeras ~2 semanas el bias condicional estará midiendo una mezcla. No es grave (el shrink del diff 03 lo amortigua) pero no te asustes si el bias rolling cambia de magnitud.

Una alternativa que consideré y descarté: meter la externa como pseudo-miembros del ensemble (mixture en vez de shift). Es más elegante probabilísticamente (ensancharía la cola caliente en vez de solo desplazarla), pero rompe la semántica de `ensemble_weights`/`eff_n`, el reweight bayesiano intradía pisaría los pseudo-miembros con residuos METAR que no les corresponden, y el panel /reweight quedaría mintiendo. El shift es lo que puedes razonar y deshacer.

Y sobre el reweight bayesiano que pediste revisar (`sigma_for_hour`, parsing de 31 miembros, truncamiento de pico): el parsing está bien (control + members, sin truncar el día — `future_today_idx` cubre hasta las 23h locales y el max por miembro incluye `max_obs`). El problema no es un bug, es estructural: el reweight solo redistribuye masa **dentro** del sobre GFS. Cuando los 31 miembros corren fríos (heatwave), reponderar elige a los menos-fríos pero el posterior sigue corto — y el softmax con SSE acumulado concentra peso en 1-2 miembros al avanzar el día (eff_n colapsa), dando confianza alta a una pred sesgada. Por eso el anclaje externo tiene que entrar *después* del reweight, como fuente de información fuera del sobre. Solo dos observaciones menores ahí: hay un bloque muerto en `build_snapshot` (~líneas 540-552, el primer loop de `past_hour_idx` que solo hace `pass`) que conviene borrar, y `peak_timing.py` usa σ=2 plano en vez de `sigma_for_hour` — inconsistencia cosmética.

---

## 4. Bet-ranking en lectura.py: el bug real detrás de KLAS NO [105-106]

Tu rec de KLAS no se escapó por el umbral — se escapó por **clasificación de dirección**. `classify_bet_direction` solo reconoce dirección en labels de cola ("or above"/"or below"); para un bin medio como "105° to 106°" devuelve `"mid"`, y `bias_blocks_bet` no bloquea nunca `"mid"`. Con pred 102.9, el bin [105-106] está entero por encima de la pred: un NO ahí es una apuesta fría de manual, y con ext_diff = 102.9 − 104.5 = **−1.6 ≤ −1.5** el bloqueo existente habría disparado... si la función hubiera sabido que era cold-side. `bets._direction` ya resuelve esto con `our_pred_f`; lectura no. El diff 05 lo replica: parsea los números del label y clasifica por posición del bin contra `s["pred"]`. Con ese parche, tu corrida del 06-10 habría impreso `🚫 KLAS: BLOQUEADO — modelo frío + cold-side (ext −1.6)`.

Los datos respaldan que los umbrales actuales están bien y el problema era de cobertura: las bets cold-direction asignan 60% y ganan 41%; en día caliente, 47% asignado contra 17% real. Mi recomendación de umbral, ya que la pides explícita: **mantén EXT_DIFF_BLOCK_THRESHOLD = 1.5 como bloqueo general direccional, y añade dos reglas de cola alta**. Primera, en régimen caliente (clim ≥ p85) baja el bloqueo cold-side a **1.0** — el miss mediano en heatwave es +2.0 °F, así que un ext_diff de −1.0 a −1.5 ya es señal suficiente de que la cola alta está viva (esto habría bloqueado también un hipotético KPHX cold-side el 06-10, donde ext_diff era −1.4 y se quedaba justo debajo del 1.5). Segunda, una regla de **proximidad al bin** independiente del ext_diff: nunca recomendar NO sobre un bin cuyo rango con padding ([lo−0.5, hi+0.5)) quede a menos de 1.0 °F de `ext_med` — tu caso KLAS es exactamente este: ext_med 104.5 tocaba el borde acolchado del bin [105-106]. Es una línea en el bloque de ACCIÓN SUGERIDA: `if direction == "cold" and s.get("ext_med") is not None and bin_lo_padded - s["ext_med"] < 1.0: bloquear`. Y en cuanto al "¿cuándo los externos overridean al modal del ensemble?": con el diff 02 esa pregunta desaparece de lectura — el modal que scrapea ya viene desplazado por el posterior, así que lectura hereda el override sin lógica propia, que es como debe ser (una sola fuente de verdad).

Lo que también encontré aquí y conviene que sepas: el bloqueo de KBOS del 06-10 en lectura **fue correcto, no un bug**. NO sobre [88+] es dirección cold; ext_diff −3.6 ≤ −1.5 dispara el bloqueo cold-side; KBOS settleó 90 y ese NO perdía. Lo confuso es el mensaje: `bias_blocks_bet` imprime "modelo frío + cold-side (bias X, ext Y)" mostrando ambos valores aunque solo uno haya disparado — arreglo cosmético, imprime solo la condición que disparó. Lo grave es lo contrario: ese gate direccional **solo existe en lectura (el CLI manual)**; `bets.maybe_bet` no lo tiene, y por eso el simulador sí colocó KBOS NO [88+] ese mismo día. El diff 04 porta el gate a `bets.py` (parámetro `ext_diff_f`, mismo umbral 1.5) — tendrás que pasarle `anchor_ctx["ext_diff"]` desde `_check_edge_alerts`, una línea. El diff 04 también corrige una asimetría existente: `_streak_blocks` clasificaba las bets históricas con `_direction(side, lo, hi)` **sin** `our_pred_f` mientras la bet nueva sí lo usaba, así que las rachas de pérdidas en bins medios direccionales nunca contaban para el bloqueo de rachas.

---

## Orden de despliegue sugerido

Si solo aplicas una cosa hoy, que sea el diff 06 (isotónica en el camino de auto-bets): es una inconsistencia pura, sin parámetros que discutir, y ataca la sobreconfianza de +19-37pp medida en la sección 1c. Después el diff 05 (dirección en lectura — es tu herramienta de decisión manual con dinero real). Luego 01+02 juntos (anclaje en posterior), después 04 (gate en bets), y al final 03 (regime del tracker, el menos urgente porque el anclaje le quita protagonismo). Tras cada paso, `pytest`: espera fallos en `test_bias_tracker.py`/`test_regime_trigger.py` (cambia worst-case por media×0.5 capada) y posiblemente en `test_bets_gate.py` (nueva firma de `maybe_bet`, retro-compatible por default=None). Y el log nuevo de ext_diff diario en `day_summary`, para que la próxima revisión pueda backtestear los umbrales en vez de razonarlos.

Archivos adjuntos: `parches_propuestos.diff` (los 6 diffs unificados, todos verificados con py_compile), `fig_calibracion.png` (reliability matinal + histograma de error por régimen) y `fig_bias_lag.png` (serie de error vs corrección del tracker, KPHX y KBOS).
