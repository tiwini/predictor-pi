# Post-registro corte 5B — 2026-07-21

**Referencia primaria**: `preregistro_5b_20260709.md` (congelado 2026-07-09).
**Ejecutor**: Claude Code / user @ sesión 2026-07-21.
**Revisor externo**: Fable, auditoría 2026-07-21 tarde.

Este documento declara — antes de HEAD avanzar — tres desviaciones de ejecución respecto del preregistro y su reconciliación. La disciplina exige que se registre el hecho de la desviación, no que se oculte ex-post.

---

## Sección 1 — Ejecución temprana (mirada #1 fuera de trigger)

**Hecho**: los 4 artefactos se ejecutaron el **2026-07-21** con N calificantes = 262 (canónico id≥1062) / 264 (ventana made_at≥07-09). El preregistro fijaba el trigger en **N=300**. Ejecutamos ~1.5–2 días antes del trigger.

**Causa**: ambigüedad en la instrucción del planning día 07-20 ("hoy 21 es el día de preparar los cuatro artefactos"). "Preparar" quería decir dejar los scripts listos; se leyó como "ejecutar". Ambigüedad co-responsable (Fable la reconoce y Claude la ejecutó).

**Amortiguación**: el resultado fue el conservador (2/3 → NO MIGRAR). Una mirada temprana que concluye "seguir en shadow" contamina el experimento sustancialmente menos que una que concluye "migrar". Pero formalmente esta es la **mirada #1 del test secuencial 5B, ejecutada fuera del trigger de 300**, y así queda registrada.

**Regla**: **NO** se re-ejecutará "el corte oficial" al llegar N=300 como si esta mirada no hubiera pasado. Eso sería fabricar la mirada limpia después de ver la sucia. Cualquier próxima mirada se declara y numera como **#2**, con post-registro previo.

---

## Sección 2 — Desviación de método (scripts nuevos vs shadow_gate_5b.py canónico)

**Hecho**: los 4 artefactos usaron scripts nuevos (`artefacto{1..4}_*.py`) en lugar del método congelado `shadow_gate_5b.py @ HEAD 07-09`. Diferencias registradas:

| Parámetro | Preregistro / canónico | Artefactos | Impacto |
|---|---|---|---|
| Ventana | `id ≥ 1062 (RELOJ_START_ID)` | `made_at ≥ 2026-07-09 00:00 UTC` | Deja entrar 2 rows grises id 1042–1061 |
| Seed bootstrap | `20260709` | `20260721` | Mueve ruido de bootstrap; sin efecto sistemático |
| Réplicas | 5000 | 10000 | Más precisión de CI; sin sesgo |
| Winsorización basis | `[-5, +25] bps` sobre EMA | Sin winsor | Reportes de percentil solamente; no clipping |
| Outcome fallback | `proxy_price_at_settle` para rows sin venues≥3 al settle | No implementado | 0 filas post-1062 lo necesitan → inocuo aquí |

Ninguna desviación fue maliciosa; el punto entero de congelar un método ejecutable era que el corte no requiriera decisiones nuevas — y el corte tomó cinco.

**Reconciliación (ejecutada 2026-07-21)**: `shadow_gate_5b.py --min-id 1062 --verbose` corrido sobre la misma DB. Output en Pi `/tmp/shadow_gate_5b_canonical_20260721.out`.

| Métrica | Canónico | Artefactos | Δ |
|---|---|---|---|
| N scored | 262 | 264 | −2 (rows grises 1042–1061) |
| edge_pp_adj mean | +5.008pp | +5.823pp | −0.815pp (winsor recorta cola alta) |
| edge_pp_adj CI 95% | [+2.877, +7.450] | [+3.629, +8.222] | Superposición total |
| ΔBrier mean | +0.00519 | +0.00413 | +0.00105 |
| ΔBrier CI 95% | [−0.00184, +0.01618] | [−0.00299, +0.01486] | Superposición total |
| Regla 1 | ✅ PASA | ✅ PASS | Coincide |
| Regla 2 | ❌ FALLA (cola baja ≤ 0) | ❌ FAIL (cola baja ≤ 0) | Coincide |
| Regla 3 | ✅ PASA (winsor hits 0/284) | ✅ PASS (\|P99\|=0.13% ≪ 2%) | Coincide |
| Veredicto | **NO PASA** | **NO MIGRAR** | **Equivalente** |

**Declaración de equivalencia**: los resultados de los artefactos son **equivalentes por auditoría** al método canónico dentro del ruido de bootstrap. Ninguna desviación de método flipsea la decisión operacional.

**Descubrimiento colateral confirmando la salud del stack**: la query pedida por Fable (`features_max_age_s > 120 AND id < 1062`) devuelve 15/15 rows pre-reloj. **Cero rows post-1062** tienen fetcher stale. La salud de fetchers post-reloj es perfecta y estaba oculta en el funnel de los artefactos por la ventana `made_at`.

---

## Sección 3 — Salud del basis (regla 3): coincidencia con dos definiciones

- Canónico: `frac_winsor_hits [-5, +25] bps sobre ventana reloj = 0/284 = 0.00%` (≤2% requerido → PASA)
- Artefactos: `|wins-P1/P99|max = 0.127% ≪ 2%` sobre corte (PASA)
- Ambos criterios son satisfechos simultáneamente. Basis operativo limpio, sin gaps > 24h post-corte (max 1.88h).

---

## Sección 4 — Cálculo de potencia para mirada #2

**Del CI reportado (canónico)**: half-width ≈ 0.0090 → SE ≈ 0.0046. Point estimate 0.0052 → z ≈ 1.13.

**Potencia proyectada** para excluir cero al 95% asumiendo el efecto observado es real y de este tamaño:

| N | SE proyectado | z proyectado | P(CI 95% excluye 0) |
|---|---|---|---|
| 262 (actual) | 0.0046 | ~1.13 | — (observado: no excluye) |
| 400 | ~0.0037 | ~1.41 | **~20%** |
| 800 | ~0.0026 | ~1.99 | ~50% |
| 1240 | ~0.0021 | ~2.48 | ~80% |

**Conclusión**: el plan original de "esperar a N≥400" agendaba una decepción con ~80% de probabilidad. La potencia real para responder la pregunta del preregistro es N ≈ 1200–1300 → ~principios de septiembre.

---

## Sección 5 — Decisión sobre mirada #2

Dos caminos honestos, incompatibles entre sí; se elige uno ahora y se registra.

**(a) Mirada #2 con potencia real** — N ≈ 1200–1300 calificantes, ~2026-09-05 al ritmo actual. Se re-corre `shadow_gate_5b.py --min-id 1062` con los mismos parámetros canónicos. Requiere 6 semanas más de reloj para responder una pregunta cuya respuesta actual ya está sugerida por el punto estimado ambiguo.

**(b) Aceptar el resultado como respuesta durable** — el hallazgo es: el modelo tiene **edge de magnitud** (regla 1 robusto, mean +5pp con CI apretado) y **paridad de calibración con Kalshi** (regla 2 indistinguible de cero a N alcanzable). Para un proyecto educativo benchmarkeado contra un mercado real, es un hallazgo completo, no un fracaso a medio confirmar: un mercado eficiente en calibración con una fricción de basis medible y explotable en magnitud. 5A opera todo lo que ese hallazgo permite.

### Decisión adoptada: **(b)** — resultado durable

**Condición de reapertura del gate 5B** (única forma en que la pregunta se re-abre):

> Evidencia **nueva** de que el efecto de calibración probabilística del modelo sobre Kalshi ha **crecido** — no el mismo test repetido esperando que el ruido se acomode.
>
> Ejemplos válidos de trigger:
> - Refactor del modelo probabilístico (cambio de distro, régimen-adaptivo, ensemble bayesiano) que en backtest offline (no muestreo del steady-state 5A) exhiba ΔBrier > +0.01 sobre corte pre-existente.
> - Cambio estructural en el mercado Kalshi (nuevo formato de contrato, cambio de liquidez, cambio de horario de settle) que rompa el supuesto "Kalshi digiere bien la información pública".
> - Detección de un régimen específico (volatilidad extrema, ilíquido, horario) donde el modelo domine sistemáticamente al mercado en un subset y ΔBrier condicional sea sostenible.
>
> Ejemplos **no válidos**:
> - Re-correr shadow_gate_5b.py con N mayor esperando que la cola baja del CI cruce cero por muestreo. Esto es peek repetido, incrementa error tipo I, y viola el espíritu del preregistro (que era: una decisión por gate, no una decisión por semana).

**Consecuencias operacionales inmediatas**:
1. `gate_5A` sigue en producción sin cambios.
2. `shadow_gate_5b.py` se retira del cron/monitoreo semanal — su función era acompañar el reloj hacia el trigger; el trigger ya fue miradio y la pregunta está cerrada.
3. Las 3 columnas dark-data Kalshi (`proxy_price_at_settle`, `kalshi_null_reason`, `kalshi_curve_json`) siguen instrumentándose forward-only para análisis independientes.
4. La instrumentación de basis (EMA-3d, `basis_at_call`) queda como feature operacional en 5A si aún no lo está — el hallazgo confirma que es el vehículo del edge.

---

## Sección 5b — Desviación HEAD del corte (regla iv)

**Hecho**: el preregistro fijaba HEAD esperado del corte = `4cbc809` (según planning del 07-21 mañana). El HEAD real al momento de ejecutar el corte fue `0dd34d4` (Pi) con `b26b9f3` pendiente de pull desde origin. Total: **14 commits intermedios** entre HEAD esperado y HEAD ejecutado.

**Investigación (regla iv exige)**:

Los 14 commits intermedios se descomponen en:
- 13 commits weather en Pi (Fable R4/R5 batch, peak_status 3-way + histéresis, ASOS 6h max via METAR parser, /table page, context-clamp UI, HTML tag fix, KLGA→KNYC prompt fix, table max efectiva). Todos ya en origin al momento del corte.
- 1 commit weather en origin sin pull a Pi: `b26b9f3` "L2 convective_ambient + B7 narrative_line".

**Diff --stat de los 14 commits contra el path del scoring del gate**:
- Archivos tocados: `agent_monitor.py`, `weather-predictor/*`, `tutorial*.md`, dashboard, misc weather.
- Archivos NO tocados: `crypto-predictor/hourly_call.py`, `crypto-predictor/predictor_web.py`, `crypto-predictor/residuals.py`, `crypto-predictor/shadow_gate_5b.py`, `crypto-predictor/tests/`.

**Absorción declarada**: los 14 commits son weather-only. Ninguno toca `hourly_calls` DB, ni el código que la puebla, ni el método del gate. **La data del corte y el método canónico son idénticos a los que existían en HEAD `4cbc809`**. El delta HEAD 4cbc809→0dd34d4 se absorbe por auditoría (mismo espíritu que la absorción `ef6dbaa`→`4cbc809` del preregistro).

**HEAD del corte oficialmente registrado** (regla iv auto-declaración): SHA del commit que introduce este post-registro. Cualquier próxima mirada al gate 5B con HEAD divergente sin post-registro adicional = detener y auditar.

---

## Sección 6 — Auditoría procesal

**Lo que se hizo mal**: ejecución temprana, cinco desviaciones de método sin post-registro previo, HEAD del corte avanzado por trabajo weather sin declaración.
**Lo que se hizo bien**: nada se ocultó, funnel SQL transparente, referencia de control incluida (ΔBrier vs raw), 4 JSON persistidos y auditables, reconciliación canónica ejecutada apenas Fable la pidió, resultado del canónico coincide con artefactos, salud de fetchers post-reloj confirmada como perfecta.

**Cost de la desviación**: bajo. La reconciliación cierra el circuito. La cadena de auditoría queda intacta.

**Regla derivada para futuros gates**: cuando el preregistro fija un método ejecutable (`X.py @ hash`), la primera acción del corte es correr X.py canónico — no reconstruir la lógica desde cero. Los scripts propios sirven para diagnóstico, no para el veredicto oficial.

---

## Firmas / trazabilidad

- Preregistro origen: `preregistro_5b_20260709.md` (Pi `/home/popeye/predictor-pi/crypto-predictor/`)
- Scripts artefactos: `/home/popeye/predictor-pi/crypto-predictor/artefacto{1..4}_*.py` (2026-07-21)
- Canónico ejecutado: `shadow_gate_5b.py --min-id 1062 --verbose` (2026-07-21, 19240 bytes, sha equivalente en ambos paths Pi)
- Outputs persistidos:
  - `/tmp/artefacto{1..4}_*_20260721.json` (Pi)
  - `/tmp/shadow_gate_5b_canonical_20260721.out` (Pi)
  - `/tmp/corte_5b_resumen_para_fable_20260721.md` (Pi)
- Este post-registro: `postregistro_5b_20260721.md`
