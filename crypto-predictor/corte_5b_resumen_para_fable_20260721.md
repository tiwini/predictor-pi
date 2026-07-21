# Corte 5B — Resumen ejecutivo (2026-07-21)

**Preregistro**: `preregistro_5b_20260709.md` (congelado 07-09, 3/3 reglas estrictas).
**Ejecutado**: 4 scripts en Pi (`/home/popeye/predictor-pi/crypto-predictor/artefacto{1..4}_*.py`).
**Sesión**: bootstrap block=24, replicas=10000, α=0.05, RNG_seed=20260721, EMA halflife=72h at-call.
**Corte**: rows `made_at ≥ 2026-07-09 00:00 UTC`, symbol=BTCUSDT.

---

## Funnel de filtros (SQL)

| Paso | N | Drop |
|---|---|---|
| all BTCUSDT | 1345 | — |
| + made_at ≥ 07-09 | 304 | −1041 |
| + settled | 303 | −1 |
| + brti_venues ≥ 3 | 302 | −1 |
| + features_max_age_s ≤ 120 | 285 | **−17** |
| + vol_regime NOT NULL | 285 | 0 |
| + kalshi strike/no válidos | 264 | **−21** |
| + kalshi_no ∉ {0,1} + σ_h > 0 | **264** | 0 |

**N efectivo = 264** (idéntico A1 y A2; 0 rows sin basis previo por herencia de 1041 rows históricos ≥ 2026-05-08).

---

## Resultados por regla

### ✅ Regla 1 — edge_adj > 0 con basis EMA-3d at-call

- edge_raw mean = **−6.47pp** (mediana −6.19pp) — sin corrección, modelo pierde.
- edge_adj mean = **+5.82pp** (mediana +5.55pp, sd 5.35pp, frac>0 = 0.86).
- **CI 95% bootstrap-block = [+3.63, +8.22] pp** — excluye cero.
- basis EMA post-corte: mean +7.7 bps, mediana +7.3 bps (estable, positivo).

**PASS**. La corrección de basis flipsea el signo del edge y lo hace robusto: el sesgo BRTI vs actual price es sistemático y aditivo al modelo.

### ❌ Regla 2 — ΔBrier(kalshi − modelo_adj) > 0

- Brier modelo_raw: 0.17797
- Brier modelo_adj: **0.17343** ← mejor de los 3
- Brier kalshi   : 0.17756
- Outcome rate (settle ≤ strike) = 0.761
- ΔBrier(kalshi − modelo_adj) mean = **+0.00413**
- **CI 95% bootstrap-block = [−0.00299, +0.01486]** — **cruza cero**.
- Referencia ΔBrier(kalshi − modelo_raw) mean = −0.00041, CI [−0.01432, +0.00951] (confirma que sin adj el modelo no vence a Kalshi).

**FAIL**. Point estimate favorable pero cola baja negativa. La ventaja de calibración probabilística existe pero es marginal a este N.

### ✅ Regla 3 — Salud del basis

- Basis global (n=1344): |wins-P1/P99|max = 0.187% ≪ 2% ✅
- Basis post-corte (n=304): |wins-P1/P99|max = **0.127%** ≪ 2% ✅
- Gaps post-corte: max **1.88h**, mediana 1.00h, P95 1.00h ✅
- Gap global 302h (2026-06-04 → 06-16, reboot/migración Pi) queda **fuera del corte**, no bloquea.
- Cobertura features intrahora post-corte: OB/taker/funding/momentum en **99.0%** non-null; features_max_age mediana 28s, P90 58s.

**PASS**. Serie utilizable limpia y estable end-to-end desde 07-09.

---

## Decisión operacional

**NO MIGRAR gate a 5B.** Cuenta 2/3. Regla #2 es el bloqueador — no aflojar el criterio ni re-interpretar el preregistro.

**Plan**:
1. Mantener gate 5A en producción.
2. Esperar a **N ≥ 400** post-filtros (~2026-08-04 al ritmo actual, ~2 semanas).
3. Re-correr los 4 artefactos con parámetros idénticos (block=24, replicas=10000, RNG_seed=20260721) para reproducibilidad.
4. Si la ampliación de N cierra la cola baja del CI de ΔBrier por encima de 0, migrar. Si no, re-diagnosticar (no re-parametrizar).

---

## Lectura de la asimetría #1 vs #2

edge_adj mean +5.82pp con CI [+3.63, +8.22] es señal fuerte de **magnitud del edge**. ΔBrier mean +0.004 con CI [−0.003, +0.015] es mejora marginal en **calibración probabilística**.

Interpretación: el modelo captura bien **dirección y tamaño** del mispricing, pero apenas empata a Kalshi en resolución probabilística puntual. Consistente con hipótesis "Kalshi digiere bien la información pública; nuestro edge viene de la corrección de basis BRTI, no de mejor calibración estadística".

Esto **no es fracaso del modelo** — es la señal esperada de un mercado eficiente donde el edge viable requiere una fricción operacional específica (basis) que Kalshi no ajusta.

---

## Artefactos persistidos

| Archivo | Contenido |
|---|---|
| `artefacto1_edge_adj_20260721.json` | edge_raw/adj stats, basis EMA stats, bootstrap CI edge_adj |
| `artefacto2_delta_brier_20260721.json` | Brier means los 3 modelos, ΔBrier bootstrap CIs (adj + raw ref) |
| `artefacto3_basis_salud_20260721.json` | basis descriptivos global/corte, gaps, feature coverage |
| `artefacto4_sintesis_5b_20260721.json` | Funnel filtros, veredicto agregado, decisión |

Ruta en Pi: `/tmp/`. Scripts: `/home/popeye/predictor-pi/crypto-predictor/artefacto{1..4}_*.py`.
