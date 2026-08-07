# Registro predictivo — 2026-08-07

Escrito **antes** de conocer los settles. Se evalúa el 2026-08-08 contra el NWS CLI.

**Lo que se pone a prueba:** en las DOS llamadas el sistema decía lo contrario.
Si aciertan, es evidencia de que la lectura del estado físico bate a la señal
automática; si fallan, de que estoy sobre-ajustando a casos vívidos, que es un
error que ya he cometido varias veces en este proyecto.

---

## 1 · KATL — bin **87-88**

Lectura a las 17:25 EDT (ventana de pico 14-17h, **cerrada**).

| | |
|---|---|
| `max_obs` | 87.1 (16:52); el feed de 5 min vio 87.8 a las 16:50 |
| current | 86.0, **30 min plana** |
| `peak_state` | 🔒 CONFIRMED |
| `ens_med` / `pred_iso_med_f` | 88.5 / 89.6 |
| externos | 84.7 |
| CLI parcial | 84.0 |
| mercado | **87-88 a 0.71**, 89-90 a 0.26 |

Gap `max_obs`→CLI en KATL (N=29): mediano **+0.00**, p90 **+1.0**, CLI>obs en
13/29. Con 87.1, tanto un gap de 0 (→87) como el del p90 (→88) caen dentro del
bin, que llega hasta 88.5.

**El sistema decía 89-90 YES con +26.5pp "✅ ACTIONABLE".** Se descarta porque
implica subir 1.2°F con el pico confirmado, la ventana cerrada, el sol bajando y
convección presente. Además `prob_rising` marcaba 1.000 con `peak_state` en
CONFIRMED — dos campos contradiciéndose.

**Probabilidad estimada: 0.75-0.85** frente al 0.71 del mercado. Poco margen.

---

## 2 · KLAS — bin **110-111**

Lectura a las 14:29 PDT (ventana 14-17h, **abierta**, 2.6 h por delante).

| | |
|---|---|
| `max_obs` | 107.1 (13:56) |
| current | 108, 28 min plana |
| `ens_med` / `pred_iso_med_f` | 108.8 / 109.0 |
| externos | 108.3 |
| régimen | heatwave, **p98** climático · ⚠ CONVECTIVE |
| mercado | 109 or below 0.43, **110-111 a 0.51**, 112-113 0.07 |

Subida de KLAS desde el `max_obs` de las 14:30 hasta el settle (N=29):
**mediana +3.0°F** (p25 +2.0, p75 +4.0, p90 +6.0). Con 107.1 la proyección es
**110.1**. Para entrar en el bin hace falta ≥ +2.4°F, y lo lograron **17 de 29
días (59%)**.

**El sistema decía vender 110-111 con +41.2pp "✅ ACTIONABLE".** Se descarta por
dos motivos: implica una subida restante de sólo +1.7 cuando la mediana es +3.0,
y la guarda de heatwave (p≥85, hoy p98) prohíbe expresamente el NO-sell de bins
altos.

**Probabilidad estimada: 0.40-0.45** frente al 0.51 del mercado. Es la mejor de
las dos opciones planteadas, **pero está cara**.

---

## 3 · KMSP — bin 80-81, **NO tomada** (control)

Se descarta a propósito, para tener contraste. Máximo 80.6 con la ventana aún
abierta 33 min y el bin cerrando en 81.5: **0.9°F de margen**, la mitad que en
KATL, y sin pico confirmado. Gap p90 de KMSP +1.1 → 81.7 → se saldría del bin.
Además KMSP tiene 2 días de 30 con gaps de settle de −12.0 y −9.0, fallos de
datos que no aparecen en KATL. Mercado a 0.57, calibrado nuestro 0.539: sin edge.

---

## Criterios de evaluación, fijados de antemano

- **KATL acierta** si el CLI da 87 u 88. Falla con ≤86 o ≥89.
- **KLAS acierta** si el CLI da 110 u 111. Falla con ≤109 o ≥112.
- **KMSP** se anota igual aunque no se tomara: si 80-81 gana, la cautela fue
  excesiva y hay que revisar cuánto peso dar al margen restante.

Marcador esperado por probabilidad: ~1.2 de 2. Cualquier resultado con N=2 es
anecdótico — **no cambiar ningún umbral con esto**, sólo acumular.

## Qué NO concluir

Que "el físico gana al sistema" con dos casos. Lo que sí es evaluable con el
tiempo: si las llamadas donde el sistema marca ACTIONABLE en contra del estado
físico fallan de forma sistemática, entonces el gate de bets necesita leer
`peak_state` y la subida restante, que hoy no lee.
