# F1 Radar — dBZ backfill + descriptive analysis

## Setup

**Fecha arranque**: 2026-07-24 (post enforce-flip 5B).

**Fuente**: Iowa Mesonet N0R composite. World file plate carree lineal
(EPSG:4326) verificado en `projection_check.py`. Mapeo naïve
`col = (lon+126)/0.01`, `row = (lat-50)/(-0.01)` correcto por construcción.

**Palette**: `dBZ = -35 + 5*idx` para idx≥1; idx=0 = missing.

## Diseño (Fable 2026-07-20)

**Estaciones (5 convectivas)**: KMIA, KIAH, KAUS, KATL, KMSY.

**Ventana temporal**: 3 semanas × 18:00-23:00 UTC. Cover 14-17 local para ET/CT.

**Cadencia**: 5-min (N0R). ~250-1260 frames total.

**Ventanas espaciales duales**: `dbz_5x5` + `dbz_9x9` en columnas separadas.
No agregar columna derivada — se computa en análisis.

**Tabla**: `radar_snapshots (station_id, ts, dbz_5x5, dbz_9x9, source)`.
`source='n0r_backfill'` para batch; `n0r_live` reservado para futuro pipeline.

## Hipótesis a probar en descriptivo (D1)

La señal más informativa para `convective_ambient` puede no ser ninguna
ventana sola, sino la **diferencia** `dbz_9x9 - dbz_5x5`:

- `dbz_9x9 alto + dbz_5x5 bajo` → storm cerca pero no encima (outflow,
  lectura sospechosa). Caso KMIA 2026-07-19 exacto.
- `dbz_5x5 alto` → precipitación sobre la estación.

**Anti-hipótesis** (a testear también): pueden hacer falta ventanas más
grandes. Sanity check 2026-07-24 mostró que en KMIA 07-19, el storm real
estaba **10km al W del pixel airport** — ni 5x5 ni 9x9 capturaron los
peaks intermitentes. Considerar `dbz_15x15` opcional en round 2 si el
descriptivo muestra falsos-negativos sistemáticos en KMIA/costeras.

## Case documented — KMIA 2026-07-19

METAR SPECI 16:19Z reportó `TS SCT035CB TSRA`. Ventana 21x21 revela:
- **Punto naive KMIA airport**: clear/-10 dBZ
- **Offset (row=2, col=-10) ~10km W**: 50 dBZ (storm real)

Interpretación: storm físicamente sobre Everglades (W de KMIA airport),
no encima del aeropuerto. Peaks intermitentes visitaron airport area
pero el core convectivo estaba desplazado.

## Consumo inmediato (post-backfill)

Notebook único L3+D+L2 sobre `radar_snapshots ⋈ station_snapshots ±5min`:
1. **D** — Viento vs error del ensemble
2. **L3** — Aplanamiento last-mile condicionado a slope ≥+1°F/h a 15h local
3. **L2** — Convective flag retroactivo (comparar `parse_convective_flags()`
   con `dbz_5x5/9x9` para calibrar threshold óptimo)

Join **versionado** en helper `join_radar_obs()` (no SQL ad-hoc repetido).

## Ejecución

```bash
cd ~/predictor-pi
./weather-predictor/venv/bin/python3 investigacion/f1_radar/backfill_radar.py
```

Rate limit: 0.2s/frame → ~10-15 min total wall clock (1260 frames × 0.2s +
overhead). Streaming: no guarda PNGs.

## Regla de la casa

- No re-ejecutar sin ver el log — INSERT OR IGNORE evita duplicates pero
  procesar 1260 frames × 0.2s consume Iowa Mesonet quota gentle.
- Ventana 5x5 puede tener falsos-negativos en KMIA/costeras (ver KMIA case).
  Ventana 9x9 mejor default para convective_ambient retroactivo.
