# Registro predictivo — 2026-07-27

Escrito **antes** de conocer el settle, y commiteado para que el timestamp de
git lo pruebe. Sin dinero de por medio: el objetivo es tener el primer punto de
evidencia sobre dos preguntas abiertas del día.

Se verifica mañana con `day_outcomes` (settle = NWS CLI final).

---

## Caso 1 — KATL: ¿el CLI parcial produce señal aprovechable?

**Estado al momento de escribir** (17:2x EDT):

```
CLI parcial      91.0°F   (emitido 16:40 local)
peak_status      🔒 pico confirmado · ventana 14-17h cerrada
max_obs          90.0 (METAR congelado 108 min) · feed 5-min llegó a 91.4
externos         90.3   ·   ext_diff -0.4   ·   convectivo SÍ   ·   eff_N 4.0/31

mercado Kalshi   92-93 → 0.57      90-91 → 0.42
nuestro sistema  92-93 → 0.266     90-91 → 0.346  (raw 0.707)
```

**PREDICCIÓN: el settle de KATL será 91°F.**
Probabilidad que le asigno: **~80% de que sea 91, ~20% de que sea 92.**

Razonamiento: el CLI es un piso duro y el pico está confirmado con la ventana
cerrada. El único camino a 92 es que el CLI temprano no capturara un máximo ya
ocurrido, que es lo que pasó el 07-26 (dijo 91, final 92) — 1 de 5 días.

**Falsación**: si el settle es ≥92, la lectura falla y el CLI temprano de KATL
no basta como piso operativo. Si es 91, el mercado estaba pagando 0.57 por un
bin con ~20% de probabilidad real.

**Lo que NO prueba**: un solo día no valida el CLI-first como fuente de señal.
Si acierta, hace falta N≥20 antes de tratarlo como edge.

---

## Caso 2 — KPHX: ¿el bias EWMA está sobre-corrigiendo?

**Estado al momento de escribir** (13:45 MST):

```
nuestra pred     110.1   (bias +3.29 APLICADO, path=ewma)
sin el bias      ~113.4
externos         110.5
mercado          111-112 → 0.60    113-114 → 0.29   (89% entre 111 y 114)
base observada   109.4 (current) · max_obs 108.0 · ventana de pico sin abrir
```

Empírico sobre 32 días de KPHX a esta hora: subida restante mediana **+4.5°F**
incondicional, **+2.9°F** condicionando a base ≥106 (N=7, r=-0.53 entre base y
subida). Ambos sitúan el cierre entre **112 y 114**.

**PREDICCIÓN: el settle de KPHX será ≥111°F**, con 112-114 como rango más
probable. El bin `109-110`, donde vive nuestra predicción, sale con 0-3% en los
dos métodos empíricos.

**Falsación**: si el settle es ≤110, el bias tenía razón y mi lectura de que un
outlier de ruptura de régimen (+8.91 del 07-26) contamina el EWMA queda
desmentida. Si es ≥111, queda el primer caso con settle real de que el EWMA
necesita protección contra outliers de régimen — pendiente abierto desde el
07-27 con N=5.

Ver `~/.claude/.../bias_ewma_outlier_regimen_kphx_2026_07_27.md`.

---

## Cómo verificar

```bash
ssh popeye@100.83.162.24
cd ~/predictor-pi/weather-predictor && ./venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect('calibration.db')
for st in ('KATL','KPHX'):
    print(st, c.execute('SELECT max_obs_f, source FROM day_outcomes '
                        'WHERE station_id=? AND date=?',
                        (st,'2026-07-27')).fetchone())"
```

---

# RESULTADO (verificado 2026-07-28 contra el CLI final del NWS)

```
KATL   settle  91.0    →  bin 90-91
KPHX   settle 113.0    →  bin 113-114
```

## Caso 1 — KATL: ✅ ACERTADO

Predije **91°F** con ~72-80%. Settle **91.0**. El CLI parcial de las 16:40 ya
traía el número final, con el pico confirmado y la ventana cerrada.

El mercado pagaba **0.36** por `90-91` y **0.61** por `92-93`. El bin favorito
del mercado perdió.

**Primer punto a favor del CLI-first como señal.** Un solo día: hace falta
N≥20 antes de tratarlo como edge.

## Caso 2 — KPHX: ✅ ACERTADO (la predicción), y el bias queda señalado

Predije **≥111, con 112-114 como rango más probable**. Settle **113.0**.

Lo que importa es la descomposición:

```
SIN bias              113.35   error +0.35°F   → bin 113-114  ✓
bias winsorizado      111.17   error -1.83°F   → bin 111-112  ✗
bias condicional      110.06   error -2.94°F   → bin 109-110  ✗
```

**El ensemble post-reweight acertó con 0.35°F.** El bias lo estropeó en las dos
versiones. La winsorización aplicada esa misma tarde (cap p95 = 6.5°F) redujo el
daño de -2.94 a -1.83 pero **no fue suficiente**: la predicción seguía cayendo
en el bin equivocado. El bias óptimo del día era ~+0.35, o sea ninguno.

Nota de limpieza: el bias se modificó a media tarde, **después** de sellar este
registro. La predicción del sistema pasó de 110.1 a 111.9 durante el día. Mi
predicción (≥111) era independiente del sistema y no se tocó.

## Qué queda abierto

Un caso no basta para desactivar el bias, pero sí para plantear la pregunta
correcta: **¿el bias tracker mejora o empeora el error, medido sobre el
histórico?** Con `pred_pre_bias` persistido se puede responder directamente.
