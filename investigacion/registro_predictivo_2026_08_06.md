# Registro predictivo — KLAX 2026-08-06

Escrito el 2026-08-06 a las 13:39 AST (10:40 local en KLAX), **antes** de conocer
el settle. Se evalúa el 2026-08-07 contra el NWS CLI.

## Por qué este día

Segundo día del corrector de nivel en KLAX. El primero (08-05) **ayudó**: nos
dejó en −1.1 cuando el crudo iba en +2.5, con settle 77.0. Hoy la señal apunta
al lado contrario.

## Estado en el momento de escribir

| fuente | valor |
|---|---|
| `ens_med` publicado (con corrector +3.30) | **77.3** |
| ensemble crudo (sin corrector) | 80.6 |
| `pred_iso_med_f` (post isotónica + blend) | 78.2 |
| mediana de modelos externos | 79.2 |
| bin favorito del mercado | **78-79 con 0.90** |
| max_obs a las 09:53 local | 73.9 (feed 5-min +1.3 por delante) |

`ext_diff` de KLAX los tres días: **+3.1 (08-04), +3.1 (08-05), −2.0 (hoy)**. El
signo se invierte el primer día con corrector, porque los externos subieron
(79.2 frente a 76-77) y el sesgo *del día* ya era menor que el histórico.

## Predicción

**El settle cae en 78-79 y hoy SOBRE-corregimos.** Tres fuentes independientes
—externos, mercado y nuestra propia distribución calibrada— están por encima del
`ens_med` corregido; sólo él dice 77.3.

## Criterios de evaluación, fijados de antemano

- **Acierto** si settle ∈ [77.5, 79.5]: la sobre-corrección se confirma.
- **Fallo** si settle ≤ 77.0: el corrector estaba bien y las otras tres fuentes
  se equivocaron a la vez.
- **Ambiguo** si settle ≥ 80: falla todo el mundo y el día no informa sobre el
  corrector.

## Qué NO concluir

Un solo día no revierte el backtest de 460 días-estación ni el resultado del
08-05. Si se confirma la sobre-corrección, lo que indica es la limitación
conocida —un offset fijo por hora no se adapta al régimen del día— y la línea a
explorar sería condicionar por `ext_diff` del día, **con pre-registro propio**.
Si falla, refuerza dejar el corrector como está.

Comparar también: `pred_iso_med_f` (78.2) contra `ens_med` (77.3). Si la
calibrada acierta y el ens_med no, es evidencia adicional para
[[revision_2026_08_11]], donde `pred_iso_med_f` lleva pendiente desde julio.
