# Weather Predictor — contexto del proyecto

App educativa (sin dinero real) que predice la temperatura máxima diaria y compara contra Kalshi prediction markets. Scope curado: **KPHX, KLAX, KLAS, KLGA, KBOS**. Usuario hispano — responder en español, conciso.

Settle alineado con Kalshi: `nws_cli.py` parsea NWS Climatological Reports (mismo source). NY (KXHIGHNY) liquida con NYC CLI (Central Park), no LGA — KLGA sigue siendo nuestro id de pred METAR.

## Arquitectura

```
predictor.py          # Núcleo: fetch, ensemble GFS 31m, reweight bayesiano,
                      # peak timing, State, Snapshot dataclass
predictor_web.py      # Flask (puerto 8000, accesible vía Tailscale 100.122.62.70)
predictor_tui.py      # Textual TUI
kalshi.py             # MarketBin dataclass + fetch_bins → Kalshi API
                      # (KXHIGHTPHX/LAX/TLV, KXHIGHNY, KXHIGHTBOS)
nws_cli.py            # Parser NWS Climatological Report — settle source
                      # alineado con Kalshi (NYC=Central Park, no LGA)
calibration.py        # SQLite: prediction_snapshots, day_outcomes, day_summary
                      # settle_day prefiere NWS CLI, cae a Open-Meteo si no hay final
isotonic.py           # PAV calibration (gated ≥20 pares, ≥7 días)
difficulty.py         # Score 0-100 combinando spread, eff_n, clim, precip, regime
multi_day.py          # D+0/D+1/D+2 day_forecast para /cross
notify.py             # ntfy.sh push (env NTFY_TOPIC)
bets.py               # Simulador P&L, auto-bet $10 cuando |edge|≥5pp
peak_timing.py        # Hora modal del pico + P(ya ocurrió)
```

## Bases de datos (SQLite)

- `market_cache.db` — precios mercado + nuestro p por bin
- `calibration.db` — prediction_snapshots, day_outcomes, day_summary, simulated_bets
- `climatology.db` — 30 años de max diarios, cacheado via Open-Meteo archive

## Comandos típicos

```bash
# Tests (101 tests, ~0.7s, sin DB/network)
./venv/bin/python3 -m pytest tests/

# Server en background (actual: ver `ps aux | grep predictor_web`)
nohup ./venv/bin/python3 predictor_web.py > web.log 2>&1 &

# Rebuild tutorial PDF
./venv/bin/python3 _build_tutorial_pdf.py
```

## Convenciones importantes

- **Max tail bins** usan `float("-inf")` / `float("inf")`, NO `-1e9/1e9` (rompe `range()` en `implied_prob_above`).
- **Per-hour σ** en reweight: `sigma_for_hour(h, station_id)` — 1.5°F pico, 2.0/2.5/3.5 según distancia.
- **Polling adaptativo**: 3 min en ventana de pico (PEAK_HOURS por estación), 10 min fuera. Invalida cache de obs durante pico.
- **Regime break detector**: obs fuera de p1-p99 con margen 4°F. ≥2 horas rotas → fuerza difficulty=100, push alert.
- **Settlement semantics**: NWS reporta °F entero; `kalshi.our_p_for_bin` aplica ±0.5 redondeo.
- **Tests sin red**: mocks `requests.get` via `unittest.mock.patch` en test_kalshi_fetch / test_nws_cli; kalshi_math usa inputs directos.

## Gotchas / "no hagas X"

- No añadir estaciones sin ponerlas en `kalshi.STATION_TO_SERIES` + `nws_cli.STATION_TO_LOCATION` + `predictor_web.SUPPORTED_STATIONS` + `PEAK_HOURS`.
- No mockear DB en tests de integración — se ha discutido, preferimos SQLite real.
- No tocar auto-bet logic sin mirar el guard contra entry_price ≤0.01 o ≥0.99.
- Puerto web: **8000** (no 5000).

## Pendientes rastreados en memoria

Ver `~/.claude/projects/-home-popeye/memory/weather_predictor_phases.md`.
