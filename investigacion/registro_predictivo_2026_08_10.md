# Registro predictivo — KSEA 2026-08-10

Escrito a las 13:02 PDT (16:02 AST), **antes** de conocer el settle. Se evalúa el
2026-08-11 contra el NWS CLI. El CLI de KSEA sale ~18:18 PDT, así que a la hora
de escribir **no hay piso de CLI**: lo que se mide es el modelo, no el piso.

## Qué se pone a prueba

El modelo y el estado físico dicen cosas incompatibles en **la estación más
fiable del roster** (KSEA: |err| 0.72°F a mediodía, 71% de acierto de bin, la
mejor de las 20 según [[fiabilidad_estaciones_2026_08_10]]).

| fuente | valor | bin implicado |
|---|---|---|
| `ens_med` | **76.8** | 76-77 |
| `pred_iso_med_f` | 78.4 | 78-79 |
| externos (mediana) | 75.7 | 75 or below |
| **proyección física** | **70.6** | 75 or below |
| favorito del mercado | 0.57 | **75 or below** |

**Proyección física:** current 63.0°F a las 13h; KSEA sube una mediana de
**+7.6°F** desde ese punto (N=29, p25 +5.8, p75 +8.2) → 70.6°F. Para alcanzar el
bin 76-77 haría falta **+12.5°F**, que ocurrió en **2 de 29 días (7%)**.

Contexto: los días que acabaron en 86-88 tenían el current en 73-75 a las 13h.
Hoy está en 63 — **diez grados por debajo**. Trayectoria de hoy: 57.2 (10:05) →
63.0 (12:40) → 62.6 (12:45), ya plana. Ventana de pico 14-17h, **aún sin abrir**,
`peak_status` en `pre-ventana`. Régimen `transition`, spread 7.4°F.

## Predicción

**El settle sale en "75 or below"** (≤75). El físico y el mercado ganan al
`ens_med`.

## Criterios, fijados de antemano

- **Físico acierta** si settle ≤ 75.
- **Modelo acierta** si settle es 76 o 77.
- **Ambiguo** si ≥ 78: fallan los dos y el día no informa.

## Lo que de verdad se evalúa: las señales ACTIONABLE

El sistema marca hoy **dos** operaciones como "✅ ACTIONABLE" y las dos van
contra toda la evidencia física:

- *vender* "75 or below" con **47.0pp** — el bin que el mercado (0.57), los
  externos y la proyección física respaldan;
- *comprar* 80-81 con **27.3pp** — con la proyección en 70.6°F.

Ambas salen de un `our_p` calculado sobre un ensemble que hoy va ~14°F por
encima del termómetro. **Si el settle confirma ≤75, es el segundo caso
documentado** (tras KATL el 08-07) de que el gate marca ACTIONABLE contra el
estado físico y se equivoca.

Acumulado hasta ahora del patrón "sistema ACTIONABLE contra el físico":
KATL 08-07 el físico ganó · KLAS 08-07 el sistema ganó · hoy pendiente.

## Corrección a mi lectura de esta mañana

A las 06:47 recomendé 76-77 diciendo que lo respaldaban cuatro fuentes. **Estaba
mal planteado**: el mercado (0.41) y los externos (76.0) de esa hora eran
lecturas provisionales que se movieron durante el día —el mercado giró de 0.11 a
0.57 en "75 or below"— así que no eran cuatro confirmaciones independientes, sino
cuatro lecturas tempranas.

También matiza lo que escribí ayer sobre que "KSEA es fiable desde las 13h": ese
83% es tasa de acierto sobre 14 días, **no** implica que a las 13h el día esté
resuelto. Hoy a esa hora la ventana de pico ni siquiera había abierto.

## Qué NO concluir

Que el modelo no sirve. Lo que se evalúa es un caso concreto de divergencia
grande (14°F sobre el current). Si el físico gana, el argumento es para **meter
`peak_state` y la subida restante dentro del gate de bets**, que hoy no los lee
aunque estén calculados al lado — no para desactivar nada.
