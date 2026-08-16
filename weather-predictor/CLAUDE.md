# Weather Predictor — contexto del proyecto

App educativa (**sin dinero real**) que predice la temperatura máxima diaria y
la compara contra los mercados de Kalshi. Usuario hispano — responder en
español, conciso.

**Roster: 20 estaciones.** `stations.py` es la fuente única — lee su docstring
antes de tocar nada del roster, lleva las doctrinas de NY y Houston.

**El Pi es la fuente canónica.** Los reportes y las decisiones salen de
`100.83.162.24`, no de la copia local (que no tiene ni venv ni las DBs vivas).
METAR o CLI directos sólo para debugging, y etiquetado como tal.

## Settle

Un solo origen: **NWS CLI** (`nws_cli.py`), el mismo que liquida Kalshi.

- `calibration.settle_day(allow_archive_fallback=False)` por defecto. El
  fallback a Open-Meteo existe pero es **opt-in y está reservado**: su max no
  coincide con el del CLI, así que meterlo en `day_outcomes` contamina la
  isotónica. Un día sin CLI se queda pendiente, no se rellena.
- El CLI vive 2-3 días en la API. Para días viejos, `fetch_max_min_cf6` /
  `fetch_month_extremes` parsean el CF6 mensual.
- **Nunca backtestear con `MAX(today_max_obs)` como proxy del settle**: difiere
  del CLI en el 70% de los días, hasta −11°F.
- `fetch_intraday_max` devuelve el CLI **parcial** del día en curso. Es un piso
  (sólo puede subir), y se consume con `max(...)` — jamás se escribe en
  `day_outcomes`.

## Arquitectura

```
predictor.py       Núcleo: fetch, ensemble GFS 31m, reweight bayesiano,
                   piso de observación, State y Snapshot dataclass
predictor_web.py   Flask :8000 (Tailscale). SUPPORTED_STATIONS viene de stations
analysis_poller.py Cycler de fondo: recorre las 20 y persiste en analysis.db
predictor_tui.py   TUI de Textual
stations.py        SINGLE SOURCE del roster: ids, series Kalshi, loc CLI,
                   PEAK_HOURS, tz, lon
```

Predicción y corrección
```
level_corrector.py Corrector de nivel por mediana causal del sesgo, POR HORA
                   (el sesgo decae durante el día). ENABLED_STATIONS con
                   backtest propio cada una: KLAX, KSFO, KNYC
bias_tracker.py    EWMA del bias — JUBILADO 2026-08-14 (EWMA_RETIRED), se
                   calcula para telemetría pero no se aplica
isotonic.py        Calibrador PAV (gated ≥20 pares, ≥7 días)
external_models.py NWS narrative + modelos externos para sanity-check
regime.py          Detector de ruptura de régimen por estación
climatology.py     30 años de máximos vía Open-Meteo archive, cacheado
```

Lectura del día y guardas
```
physical_gate.py   Techo físico: veta bins que el termómetro ya no permite
peak_window.py     Distribución empírica de la hora del pico (últimos 7 días)
peak_timing.py     Hora modal del máximo + P(ya ocurrió)
agent_signals.py   Señales y gates por bin, compartidos por las herramientas
difficulty.py      Score 0-100 ⚠ REVOCADO como gate (N=505: no predice el error)
divergence.py      Divergencia D+1/D+2 · overnight.py  skip flag nocturno
streaks.py         Rachas de acierto por estación × ventana horaria
weather_alerts.py  NWS Active Alerts — frentes fríos, tormentas severas
```

Mercado y P&L
```
kalshi.py          MarketBin + fetch_bins; our_p_for_bin aplica el ±0.5
bets.py            Simulador: EDGE_THR=0.05, STAKE=$10 hipotéticos
bets_sweep.py      Sweep retrospectivo sobre simulated_bets
brier_watchdog.py  Vigila el Brier (respuesta a la auditoría del ledger)
```

## Bases de datos (SQLite, las vivas están en el Pi)

- `analysis.db` — **la grande (~770MB)**. `station_snapshots` (la serie que
  alimenta casi todo backtest), `kalshi_snapshots`, `radar_snapshots`
- `calibration.db` — `day_outcomes` (settles), `prediction_snapshots`,
  `simulated_bets`, `day_summary`, `brier_weekly`, `daily_skip_flags`…
- `market_cache.db`, `climate_cache.db`
- ⚠ `climatology.db`, `kalshi.db` y `predictor.db` están a 0 bytes: muertas

Ojo con la asimetría que ya costó un diagnóstico: `prediction_snapshots` la
llena `predictor_web`, que tiene **una sola estación activa a la vez**;
`analysis_poller` escribe a `analysis.db` y sí cubre las 20. Cualquier feature
que lea de la primera funcionará sólo en la estación que el web tenga abierta.

## Comandos

```bash
# Tests: 328, ~3s, sin red ni DB (correr en el Pi, no hay venv local)
./venv/bin/python3 -m pytest tests/ -q

# Servicios: kill por PID tras verificar cwd, NUNCA por nombre
nohup ./venv/bin/python3 predictor_web.py   >> web.log 2>&1 &
nohup ./venv/bin/python3 analysis_poller.py >> analysis_poller.log 2>&1 &

# Lectura operativa de una estación (predicción, físico, gates, bins)
./venv/bin/python3 ../investigacion/lectura_estacion.py KPHX
```

## Convenciones

- **Bins de cola** con `float("±inf")`, NO `±1e9` (rompe `range()` en
  `implied_prob_above`).
- **σ por hora**: `sigma_for_hour(h, station_id)` — más ceñida cerca del pico
  para que las obs ruidosas del amanecer no dominen el reweight.
- **Polling adaptativo**: 3 min dentro de `PEAK_HOURS`, 10 min fuera.
- **Settlement**: el NWS reporta °F entero; `our_p_for_bin` aplica el ±0.5.
- **Compute-once**: lo que el `Snapshot` calcula se calcula una vez y se pasa;
  y si una guarda va a necesitarlo, **persistirlo** — ya pasó dos veces que el
  dato existía en memoria y no en la tabla (`current_temp_stable_min`,
  `current_obs_ts`).
- **Tests sin red**: mock de `requests.get` vía `unittest.mock.patch`.

## Gotchas / "no hagas X"

- **Estación nueva → sólo `stations.py`**, una línea. Los 5 consumidores la
  heredan por import. Y auditarla contra el `result` de Kalshi de un día ya
  liquidado antes de fiarse (Houston liquidaba con Hobby, no con Bush).
- **Nunca reintroducir `KLGA`.** La estación de NY es `KNYC` (Central Park).
- **Nunca matar procesos por nombre**: weather y crypto corren ambos
  `predictor_web.py`, distinto cwd. Verificar `/proc/<pid>/cwd` primero.
- **El crypto (:8001) corre bajo systemd**: `sudo systemctl restart
  crypto-predictor`, nunca kill+nohup. El weather (:8000) sí es nohup.
- **Añadir a `ENABLED_STATIONS` exige backtest pre-registrado propio.** El test
  del roster lo assertea entero para que romperlo sea deliberado.
- No mockear la DB en tests de integración — preferimos SQLite real.
- No tocar el auto-bet sin mirar el guard de `entry_price` ≤0.01 / ≥0.99.
- Puerto web: **8000** (no 5000).

## Doctrina de medición

Antes de aplicar cualquier señal nueva, esto ya se ha aprendido a base de
falsos positivos:

- **Pre-registrar el criterio y commitearlo antes de correr** el backtest.
- **Decidir con rho DENTRO de estación + test de signos.** El pool cruzado
  exagera: 2 de 3 backtests habrían dado falso positivo con él.
- **No reutilizar la base de julio**, que lleva doce pasadas. Días frescos.
- **N<10 no cambia un threshold.**
- Verificar las afirmaciones de reviewers externos (Fable/Codex) contra el
  código antes de aplicarlas — extrapolan sin ver el repo.

## Pendientes

En la memoria del usuario: `~/.claude/projects/-home-popeye/memory/MEMORY.md`,
sección "Pendientes abiertos".
