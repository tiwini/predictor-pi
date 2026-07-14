# Pre-registro 5A/5B — congelado 2026-07-09

Texto frozen para el reloj N=300 del gate R6 de migración de target del
modelo. Committeado ANTES de que se acumule un solo row post-R8 para
neutralizar cualquier sospecha de p-hacking.

## Cadencia confirmada del retry pass

- `predictor_web.py:37`: `POLL_SEC = 5`
- `predictor_web.py:529`: `time.sleep(POLL_SEC)` en `poll_loop`
- ⇒ 720 iteraciones/hora de `settle_due`
- Sin throttle previo, el retry ampliado de R8 hubiera disparado ~720
  refetches/row/hora para toda row con `n_venues < 4` en la ventana de 1h
  post-settle. El fix R9 (gate 5 min + fetch faltantes + ventana upgrade
  15 min) acota esto a ≤12 pasadas/hora × 1 row promedio = tamaño manejable.

## 5A — BRTI-mediana como oráculo de análisis shadow

**Ya adoptada, sin experimento.** La mediana multi-venue es por definición
mejor aproximación al settle Kalshi que un constituyente solo — mejora de
instrumento de medición, no hipótesis.

**Condición única de calidad de datos**:
- `n_venues ≥ 3` en ≥ 80% de rows del shadow window.
- **Stopping rule**: si `n_venues < 2` en cualquier row, o si el ratio
  `n_venues≥3` cae bajo 80% en ventana móvil de 24h, la decisión del gate
  5B se pospone con diagnóstico.

**Estado post-fix + backfill (2026-07-09 ~14:30 UTC)**: 13/14 R7-era en
n=4, 1/14 en n=3 → 100% ≥ 3. OK.

**Análisis shadow offline** usa `brti_proxy_price` cuando no-null, fallback
etiquetado a `proxy_price_at_settle` (Coinbase) para rows pre-R7. Zero
runtime change.

## 5B — Gate R6 para migración de target del modelo

Reglas **3/3 obligatorias** para migrar:

1. **`edge_adj` con basis EMA-3d at-call > 0**, CI bootstrap-block
   excluyendo cero.
2. **ΔBrier vs Kalshi > 0**, mismo método bootstrap-block.
3. **Salud del basis operativo**: ≤2% winsorizado; sin gaps > 24h de
   fetchers sub-horarios (OB / taker / funding). FNG excluido del gate
   por ser horario y no sub-horario.

**Outcome de scoring**: `brti_proxy_price` (instrumento mejor, 5A ya lo
adoptó como referencia), con fallback etiquetado a Coinbase para rows sin
brti.

**Explícitamente NO reintroducimos**:
- `kurt ∈ [3, 9]` — demolido en R3 con 84% de FP a N=72; a N=300 el sesgo
  del estimador con colas pesadas sigue siendo severo.
- `PIT-BRTI` como gate del modelo actual — falla por construcción (el
  modelo predice Binance, ya sabemos que sus cuantiles corren ~10pp en
  espacio BRTI). Es criterio del experimento POST-migración
  ("WR vuelve a 0.70±2SE en N=150", ya registrado en R6).

**PIT dual offline**: computamos ambos (Binance y BRTI) por barato +
input de diseño para la migración. Gate pre-registrado vive en espacio
Binance.

## Reloj N=300

- **Inicio**: 2026-07-09 con el primer `make_call` R8-clean tras restart
  de crypto en :8001 (PID nuevo tras rsync de fixes R9).
- **Corte estimado**: ~2026-07-21 (12-13 días naturales al ritmo 1/h ≈
  24/día).
- **Filtros de exclusión pre-corte** (deciden fuera de banda antes de
  ver outcomes):
  - Rows con `brti_proxy_n_venues < 3`.
  - Rows con `features_max_age_s > 120s` (fetcher stall).
  - Rows con `vol_regime_ratio = NULL` (historia insuficiente <330 klines).

## Firma de congelamiento

Commiteado en `predictor-pi` HEAD post-R9 restart. Hash del commit y
diffstat quedan en `git log`. Cualquier cambio a este archivo tras esta
fecha requiere entrada nueva de post-registro con timestamp y motivación
explícita.

---

## Post-registro 2026-07-09 ~16:00 UTC — método canónico gate 5B

**Motivación**: Fable R10-review 2026-07-09 detectó ambigüedad — las
columnas de shadow (`basis_at_call`, `edge_pp_adj`, `model_no_at_strike_adj`,
`won_proxy`) no existen en el schema. El pre-registro original evaluaba el
criterio 1 sobre `edge_adj con basis EMA-3d at-call`, referencia sin
fuente de datos. Fijamos el método canónico ANTES de que se acumule un
solo row calificante bajo la ambigüedad.

**Decisión**: criterios 1 y 2 del gate 5B se computan con
`shadow_gate_5b.py` (reconstrucción offline determinista). El basis EMA
se reconstruye a partir de observaciones de settle previas al `made_at`
de cada row (fuga temporal cero). El script es puro determinismo — mismo
DB + mismo commit → mismos números.

**Fuente de datos**:
- basis_bps observado = `1e4 * (actual_price − outcome_price) / actual_price`,
  con `outcome_price = brti_proxy_price` cuando `n_venues≥3`, fallback
  etiquetado a `proxy_price_at_settle` (Coinbase) para rows pre-R7.
- basis EMA time-aware, HL=3d, winsor [-5, +25] bps, evaluado a
  `made_at[i]` con SÓLO obs con `settled_at < made_at[i]`.
- df=4 para id<966, df=5 desde id 966 (mismo switch que
  `basis_timelocal_r5.py`).

**Bootstrap**: block bootstrap con `block=24` (una semana horaria),
5000 replicates, CI 95% percentil 2.5/97.5, seed fija 20260709.

**Filtros de exclusión** (idénticos al pre-registro original): aplicados
DESPUÉS del cómputo del basis histórico (usa toda la historia disponible)
pero ANTES del scoring del gate:
- `brti_proxy_n_venues < 3`
- `features_max_age_s > 120`
- `vol_regime_ratio IS NULL`

**Referencias congeladas** (commit con esta entrada):
- `crypto-predictor/shadow_gate_5b.py` — implementación canónica.
- Reglas de winsorización, HL, df-switch, bootstrap block/N/seed son
  constantes al tope del script; cualquier cambio requiere nuevo
  post-registro.

**Estado en la primera corrida contra DB actual** (2026-07-09 ~16:00
UTC, min-id=1): `N scored = 0`. Todas las filas pre-R7 excluidas por
`n_venues<3` (schema anterior sin multi-venue); las 11 filas R7-era
excluidas por `features_max_age_s > 120` (valor stored pre-R8 fix
contaminado por edad de FNG). Cero evidencia leakeada, gate ambiguo
resuelto.

**Contador de exclusiones** (Fable R10 punto 3): `shadow_gate_5b.py`
imprime los 3 contadores (n_venues<3, max_age>120, vol_regime=NULL)
en cada run. Correr semanalmente durante el reloj N=300; si la tasa
total de exclusión supera 5% del emitido, la fecha de corte se recalcula
y el patrón de exclusión se investiga antes del gate.

**Definición del reloj**: N=300 son filas **calificantes** (pasan los 3
filtros), no filas emitidas. La última hora del corte siempre tiene 1-2
pendientes de settle — el corte se ejecuta cuando el contador de
calificantes toca 300.

---

## Post-registro 2026-07-09 ~17:00 UTC — Fable R11 gaps cerrados

**Motivación**: Fable R10-review detectó tres huecos en el método
canónico que había que fijar mientras N=0. Se aplican todos como una
sola entrada de post-registro atada al mismo commit del código.

### (a) Cuarta categoría de exclusión: kalshi_book

`shadow_gate_5b.py` R10 filtraba silenciosamente en SQL las rows con
`kalshi_strike IS NULL` o `kalshi_no_at_strike ∈ {0.0, 1.0}`. Eso
funcionaba pero dejaba la definición implícita — decisión "en el corte,
¿cuentan para N=300?" quedaba tomada post-hoc.

R11: el filtro se declara explícito como cuarta categoría
`kalshi_book`, visible en `excl_counts`. Historia R6 indica ~15-20% de
rows sin book válido (concentrado madrugada UTC = estructuralmente
no-aleatorio).

Definición: row **calificante** requiere los 4 filtros:
1. `brti_proxy_n_venues ≥ 3`
2. `features_max_age_s ≤ 120`
3. `vol_regime_ratio` NOT NULL
4. `kalshi_strike` NOT NULL AND `kalshi_no_at_strike` ∉ {0.0, 1.0}

**Precedencia**: primer bucket que dispara consume el row (single-count).
Orden literal 1→4 arriba. Cambiar el orden requiere post-registro
adicional.

**Separación criterio 3**: la salud del basis (fracción winsorizada,
gaps de fetcher) se evalúa sobre **todas** las settleadas, no sólo
calificantes — no necesita Kalshi book. Ya implementado así en R10.

### (b) Modo `--filters-only` — anti-peeking del edge

Corridas semanales del contador exponen el edge acumulándose (bug de
disciplina detectado por Fable R10). Fix por código: flag
`--filters-only` computa y reporta sólo `excl_counts` + N calificante,
sin tocar edge ni ΔBrier.

**Protocolo de uso durante el reloj**:
- Monitoreo semanal → `shadow_gate_5b.py --filters-only`.
- Corrida completa (edge + ΔBrier + gate) → única, al llegar a N=300
  calificantes.

Cualquier corrida completa antes del corte queda como violación de
la disciplina de espera y debe declararse en post-registro.

### (c) Aritmética corregida del reloj

Con tasa de exclusión steady-state esperada ~20% (15% Kalshi book +
5% resto), N=300 calificantes ≈ 375 emitidas ≈ 15.6 días desde
2026-07-09 al ritmo 1/h. **Corte estimado revisado: 2026-07-24/25**
(antes 2026-07-21 asumía exclusión cero, incorrecto).

### (d) Fuente mixta del EMA (nota, sin acción)

Las primeras rows calificantes reciben un EMA-3d cuyos aportantes
tempranos vienen mayormente de la historia Coinbase-fallback, con
transición gradual a BRTI-mediana. El escalón de fuentes es ~0.24 bps
(medido R6) contra RMSE one-step del basis ~1.4-3 bps → inmaterial y
determinista. Etiquetado por `outcome_src` por row. Se anota aquí para
que no aparezca como "anomalía" en el corte.

### (e) Precedencia del contador (nota, sin acción)

`excl_counts` es single-count con precedencia por orden literal 1→4.
Row que dispara múltiples filtros aparece sólo en el primero. Suma de
las 4 categorías + N calificante = total settleadas.

---

## Post-registro 2026-07-09 ~18:00 UTC — Fable R11 gaps

**Motivación**: R11-review detectó 3 contradicciones o defectos en las
piezas recién commiteadas. Corrige antes de que la data acumulada
convierta cualquiera en falso positivo/negativo perpetuo.

### (i) Triggers de exclusión por bucket, no flat >5%

El trigger "tasa total > 5%" contradice la aritmética del reloj
(esperado steady-state ~20%). Alarma que siempre suena = alarma que
nadie escucha (misma patología que los 4 tests rojos "pre-existentes"
que R7 arrastraba). Separación:

- `kalshi_book > 30%` en ventana semanal → investigar. Histórico
  R6 = 15-20%; 30% es degradación real del book Kalshi o del fetcher
  de curvas.
- `n_venues<3 + max_age>120 + vol_regime=NULL` combinadas `> 5%` →
  investigar (post-warm-up estas deben ser raras).
- `total > 35%` → recalcular fecha de corte (no sólo investigar).

El aviso simple `> 5%` que emitía `report_filters_only` R11 queda
reemplazado por estos tres. Cambio en el código del script + esta
entrada.

### (ii) Ritual de salud validado contra positivo conocido

Los 5 fallos de arranque de systemd de esta tarde (14:36-14:38 AST) por
puerto tomado emitieron `Address already in use` **sin traceback** —
Flask/werkzeug lo formatea plano, no como stack. El ritual R11 que
grepeaba `-ci traceback` reportó **0** sobre 7 días, ocultando un
positivo conocido.

Test contra el positivo (2026-07-09 ~17:45 UTC):

| Pattern                              | Count hoy |
|--------------------------------------|-----------|
| `traceback` (-ci)                    | 0         |
| `Main process exited`                | 5         |
| `Failed with result`                 | 6         |

`Failed with result` es el métrico completo (captura fallos
individuales + burst-limit del `StartLimitBurst=5/10min`).

**Ritual corregido**:

```bash
# 1. Estado actual del servicio
sudo systemctl show crypto-predictor \
    -p NRestarts,MainPID,ActiveState,ExecMainStartTimestamp

# 2. Fallos systemd en 7 días (pattern validado contra el positivo del
#    2026-07-09, no depende de 'traceback' que Flask no emite en bind)
sudo journalctl -u crypto-predictor --since -7d | \
    grep -c 'Failed with result'

# 3. Tracebacks Python (crashes runtime distintos de bind — mantener
#    aunque hoy sea 0, es el detector para el próximo crash tipo 07-07)
sudo journalctl -u crypto-predictor --since -7d | grep -ci traceback
```

**Trigger de acción**:
- `Failed with result` > 0 en 7d → investigar.
- `traceback` > 0 en 7d → investigar.
- `ActiveState != active` → acción inmediata.
- `ExecMainStartTimestamp` más reciente que el chequeo anterior sin
  restart voluntario → hubo un reinicio invisible al counter (contorna
  el reset de `NRestarts` en reboot de la Pi).

### (iii) Nota histórica para el postmortem

Los reportes R8, R9, R10 describían "crypto systemd :8001" cuando las
PIDs reportadas eran nohup, no systemd. La generalización del
postmortem del typo de timeouts (R10): **estado del ambiente se reporta
desde comandos, no desde intención**. `systemctl show -p MainPID` es la
fuente de verdad de "bajo systemd", igual que `grep -n` es la fuente de
verdad de constantes del código. Regla persistida en memoria del AI.

Consecuencia colateral: las calls emitidas bajo mis nohup zombie de hoy
corrieron sin `PYTHONUNBUFFERED` / `PYTHONFAULTHANDLER` (esas env vars
viven en la unit, no en mi nohup). Cero impacto en la data (rows
idénticas), pero un crash de esta tarde habría dado un forense pobre
tipo 07-07. Otra razón por la que la reconciliación importaba, aparte
del NRestarts artificial.

## Post-registro R13 (2026-07-09 ~19:00 UTC)

Cierre de Fable R12: los umbrales bucket que fijamos en R12
(kalshi>30%, no-Kalshi>5%, total>35%) se computaban sobre TODAS las
settleadas cargadas, incluidas las ~1042 rows pre-R7 con `n_venues<3`.
En steady-state post-R8-clean con 375 rows emitidas nuevas, el
denominador sigue siendo `1042/(1042+375) ≈ 73%` para "n_venues<3" —
el trigger "no-Kalshi > 5%" se dispararía perpetuamente por diseño,
convirtiendo la alerta en ruido de nuevo (misma clase de defecto que
el flat >5% que R12 corrigió, un nivel arriba). La historia pre-reloj
no puede salir de la DB, así que la corrección es de definición: los
denominadores son sobre la ventana del reloj, no sobre toda la DB.

### (i) Constante RELOJ_START_ID

Fijada como constante literal en `shadow_gate_5b.py`:

```python
RELOJ_START_ID = 1062  # primer id bajo systemd PID 15504
                        # (post-reconciliación 2026-07-09 16:29:45 AST)
```

Verificado por DB: `id=1061` fue el último row bajo el nohup zombie
PID 13306 (made_at=2026-07-09 20:00:04 UTC), antes de la
reconciliación a las 20:29:45 UTC. La próxima call horaria (id=1062,
esperada ~21:00 UTC) es la primera bajo el sistema systemd. Cambio de
esta constante requiere post-registro adicional; en particular, si
hay que reconciliar de nuevo el servicio, el nuevo RELOJ_START_ID
debe anotarse aquí sin recomputar hacia atrás.

### (ii) Denominadores de monitoreo restringidos a ventana reloj

En modo `--filters-only`:

- Los rows con `id < RELOJ_START_ID` se cuentan como **legacy** y se
  reportan en una línea informativa aparte con el desglose por bucket
  ("legacy pre-reloj: N rows, exclusiones [n_venues<3=..., ...]").
- Los rows con `id >= RELOJ_START_ID` forman la ventana reloj. Los
  porcentajes por bucket y el TOTAL se computan sobre ese N reloj.
- Los triggers de alerta (kalshi>30%, no-Kalshi>5%, total>35%) se
  evalúan sólo sobre la ventana reloj.
- Si `N_reloj == 0` (etapa actual), los triggers no se evalúan y se
  reporta explícito ("sin rows en ventana reloj todavía — triggers no
  evaluados").

### (iii) Criterio 3 del gate: frac_winsor sobre ventana reloj

Fable notó paralelamente que el criterio 3 ("salud del basis sobre
todas las settleadas") también debía leerse como "todas las
settleadas de la ventana shadow" — el ≤2% winsorizado no debe
diluirse ni contaminarse con los 60 días históricos. Aplicado:

- `build_basis_history` sigue construyendo `obs` sobre TODA la
  historia (la EMA time-aware necesita las observaciones legacy para
  el arranque en frío del basis).
- Contadores `n_winsor_reloj` y `n_total_reloj` sólo cuentan rows con
  `id >= RELOJ_START_ID`.
- El reporte de criterio 3 usa `frac_wins = n_winsor_reloj /
  n_total_reloj`.

### (iv) Gate 1-2 en corrida completa

Sin cambio semántico: las filas scoreadas siguen siendo las que pasan
los 4 filtros. Por diseño el filtro `n_venues<3` excluye toda la
porción pre-R7, así que las qualifying rows son naturalmente
post-R8. En el flujo de código el input al gate 1-2 se restringe
explícitamente a `scored_reloj` (por defensa en profundidad: una fila
legacy que por accidente pasara los 4 filtros no debe alimentar el
gate 1-2).

### (v) Racional para no volver a corregir el post-registro

Fable R12 lo dice explícito: "esto es una entrada más de
post-registro — la última, espero, porque con N=0 todavía es
corrección de diseño y no ajuste post-hoc; en una semana ya no lo
sería". Este R13 es la corrección final admisible al método pre-data.
Cualquier ajuste ulterior antes del corte requiere trigger legítimo
declarado en R11-R12 (fallo de fetcher, bug en código que alimenta el
scoring, o alerta del ritual sistémico) y se declara como
post-registro adicional con explicación explícita.

## Post-registro reconciliación divergencia Pi↔origin (2026-07-13 ~21:00 UTC)

**Trigger:** Fable R4 close-out review levantó que el HEAD crypto en
Pi era `fb2a596` (R12) mientras origin tenía `ce9c2a2` (R13). No era
typo — R11 (`faec493`) y R12 (`fb2a596`) vivieron sólo en Pi durante
4 días sin sincronizar con origin. R13 se creó en origin desde
`f2acbe0` sin ver R11/R12. Weekly monitoring corrió con los
denominadores del R10 rotos desde 07-09 hasta 07-13 (el trigger
"no-Kalshi > 5%" nunca disparó porque nadie lo evaluó — el script R12
en Pi no tenía la aritmética reloj-restringida de R13).

**Auditoría del contenido (07-13 tarde, `git diff fb2a596 ce9c2a2`):**
- `preregistro_5b_20260709.md`: 0 deleciones, 65 adiciones. El
  contenido R11+R12 (filters-only, kalshi_book, ritual salud
  validado, triggers por bucket 30/5/35) se preserva íntegro en la
  versión R13. La sesión R13 partió del archivo tal como estaba en
  Pi al escribir R11/R12, no de `f2acbe0`. Sin pérdida documental.
- `shadow_gate_5b.py`: 38 deleciones, 82 adiciones — refactor puro.
  Funciones `build_basis_history`, `report`, `report_filters_only`
  reemplazadas por versiones con `RELOJ_START_ID` awareness.
  Precedencia 1→4 (líneas 21/220/403), umbrales 30/5/35 (424/427/430),
  `--filters-only` sin cómputo de edge (funciones separadas 372).
  Sin regresión semántica.
- Smoke test 07-13 `--filters-only --verbose` en Pi tras adopción:
  legacy 1061 rows segregados en línea informativa; ventana reloj
  N=100 (kalshi_book=5.0% justo en el trigger); N calificantes=95;
  triggers no evaluados aún (`N reloj < 300`).

**Acción aplicada:** `git checkout ce9c2a2 -- shadow_gate_5b.py
preregistro_5b_20260709.md` en Pi → commit `2fe3978` "adopt R13
supersede Pi-local R11/R12" → `sudo systemctl restart
crypto-predictor` (registrado como restart voluntario, sección (i) más
abajo) → HTTP 200 tras warm-up.

### (i) Restart voluntario 2026-07-13 ~21:07 UTC — adopción R13

Anotado para que el ritual del lunes (`ExecMainStartTimestamp` más
reciente que el chequeo anterior sin restart voluntario → investigar)
no interprete el `sudo systemctl restart crypto-predictor` de las
21:07 UTC como reinicio invisible. Motivo: cargar el nuevo
`shadow_gate_5b.py` con `RELOJ_START_ID=1062` (aunque
`shadow_gate_5b.py` corre por cron, no por systemd — el restart del
Flask fue conservador para asegurar caches vacíos, no técnicamente
necesario). PID nueva: sujeta al chequeo del lunes.

### (ii) Regla de disciplina post-incidente

Persistida en memoria del AI 2026-07-13:
- Estado del repo se reporta desde `git log --graph --oneline -5` +
  `git status -sb` (que muestra ahead/behind contra origin), nunca
  desde narrativa de la sesión. Un `--graph` en R12 habría mostrado
  la divergencia el día que nació.
- Push a origin es parte del close-out, no un after-thought. Pi es
  runtime; origin es canonical. Convergencia Pi↔origin queda como
  operación pendiente en este mismo close-out.

### (iii) Racional para reset Pi→origin/main tras este post-registro

Push desde Pi arrastraría a origin 11 commits (R11/R12 exploratorios
+ 3 doppelgängers weather + 1 doppelgänger tokens + `2fe3978`
redundante — su contenido = `ce9c2a2`). Reset Pi a `origin/main`
converge la historia sin contaminar origin. Los R11/R12 quedan
reconocidos en el commit message de `ce9c2a2` ("Fixes Fable R12
audit") — trazo histórico sobrevive en la narrativa canónica.
Uncommitted work en Pi (5 M + varios ??) se preserva vía stash.

### (iv) HEAD esperado en el corte

HEAD del corte = `ef6dbaa` (este post-registro), salvo post-registros
adicionales declarados; cualquier otro delta = investigar. El hash
`ce9c2a2` referido arriba es el R13 canónico previo a esta
reconciliación — quedó absorbido por `ef6dbaa` como parent directo en
origin/main. Si al ejecutar el corte el HEAD difiere de `ef6dbaa` sin
un post-registro que lo justifique, detener el corte y auditar.

## Post-registro snapshot R6-R8 productivo (2026-07-13 ~22:30 UTC)

**Trigger:** Fable R4 close-out review #4 detectó que
`crypto-predictor/hourly_call.py`, `predictor_web.py`, `residuals.py`,
`start_all.sh` + tests corrían en Pi sin commit — modificados desde
07-06/07-09 con el bloque de features intraday que alimenta el gate.
"No relacionado" era prosa mía; el diff nombra explícitamente el path
del scoring del gate.

**Contenido del snapshot (Pi → origin, este post-registro):**
- **R6 (07-06 aprox.):** BRTI proxy multi-venue en `hourly_call.py`
  (`_multi_venue_prices`, Coinbase+Bitstamp+Kraken+Gemini secuencial con
  budget 15s), `statistics.median` de constituyentes, 10 columnas
  `ALTER TABLE hourly_calls` (bitstamp_price_at_settle,
  brti_proxy_price, brti_proxy_n_venues, momentum_pct_per_min,
  ob_imbalance, taker_buy_ratio, funding_rate, fng, vol_regime_ratio,
  features_max_age_s). `make_call(features=...)` persiste snapshot al
  disparo del call.
- **R7-review Fable (07-09):** retry pass reescrito — filtro cambia de
  `brti IS NULL` a "cualquier venue-col NULL o n_venues<4" (retry pre-fix
  ignoraba rows con brti poblado pero Coinbase NULL); `COALESCE` protege
  mediana ya escrita. En `predictor_web.py`: warmup EWMA seed = varianza
  muestral primeros 30 r² con mínimo 330 klines (5 slow-half-lives); OB
  imbalance rescalado a [-1, +1] (spec crudo); FNG excluido de
  `features_max_age_s` (su TTL diario dominaba métrica y la volvía
  ciega a fetchers muertos).
- **R8-review Fable (07-09):** throttle module-level
  `_last_retry_scan_ts` con `_RETRY_SCAN_MIN_INTERVAL_S=300s`
  (poll_loop corre a POLL_SEC=5 → sin throttle serían ~720
  refetches/hora/row); `skip=` param en `_multi_venue_prices` para no
  re-fetchear venues ya poblados en candles históricos deterministas;
  `_RETRY_UPGRADE_WINDOW_S=900s` y `_RETRY_RESCUE_WINDOW_S=3600s`
  (dos ventanas separadas de retry); adaptive timeout último venue
  aprovecha budget sobrante.
- **`residuals.py`:** `analyze()` acepta `--min-id` para excluir historicals
  pre-R6 al re-correr el PIT.
- **`start_all.sh`:** línea `start_crypto` comentada — crypto migró a
  systemd (`crypto-predictor.service`) el 07-08.
- **`tests/`:** 34 tests pasan en Pi (`test_hourly_call.py` +
  `test_vol_regime.py`), incluyendo regresión inmortalizada del bug
  R7 pre-fix.

**Impacto sobre el corte:** el gate 5B corre sobre `hourly_calls` DB
poblada por este código desde 07-06 en adelante. El artefacto "hash
confirmando `ef6dbaa`" ahora sí certifica el código que produjo la
data — pre-commit el hash certificaba HEAD divergente del working tree
productivo. Regla (iv) HEAD del corte pasa de `ef6dbaa` al SHA de este
commit (auto-declarado).

**Untracked Pi sin migrar** (scripts one-shot investigación R5-R7,
backfills, comparaciones venue): quedan como TODO separado, no
alimentan `hourly_calls` DB → no afectan corte. Se decide destino
(commit histórico en branch, discard, o commit main tardío) sin
bloquear cronograma.

**How to apply:**
- Antes de decir "no relacionado" sobre archivos M, `git diff --stat`
  literal + identificar el path por función productiva. La palabra
  "hourly_call.py" en un `git status` del árbol de scoring **es** el
  path del gate por definición.
- Cierre de sesión con uncommitted en la Pi = violación de disciplina
  de convergencia, no "ordenaremos mañana". El test operativo antes de
  irse: `git status -sb` limpio + `git stash list` vacío.

## Post-registro scripts investigación R5-R7 (2026-07-13 ~23:00 UTC)

Movidos 10 scripts one-shot (backfills, brti_comparison_r6, ema_rmse_r6, naive_rmse_r7, venue_probe_r6, verify_basis_edge, basis_timelocal_r5, bootstrap_and_hourly_r5) a `crypto-predictor/investigacion/` con README. Producen la evidencia de constantes pre-registradas (half-life 3d, edge ajustado, baseline naive) — auditor del corte puede correrlos, no citarlos. `residuals_min_id.patch` y `start_all.sh.bak_20260708_195010` borrados (redundantes con contenido ya committeado). Regla (iv): HEAD del corte avanza al SHA de este commit.
