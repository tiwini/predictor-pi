# Registro predictivo — KDEN, 2026-08-25

Escrito a las **15:36 MDT con el día ABIERTO** (ventana de pico 13-18h), antes
de conocer el settle. Se puntúa cuando salga el CLI.

## Estado en el momento de escribir

```
observado        82.0°F a las 14:53 local   (+3.9°F en una hora)
                 78.1 · 13:53   75.0 · 12:53   73.0 · 11:53
piso             82.04°F
nuestro ens_med  83.5°F      banda p10-p90  83.3 – 83.8   (0.5°F)
ensemble         500 miembros, de 82.3 a 84.3 — CERO por encima de 85
techo físico     87.0°F      (max_obs + p90 de subida restante)
externos         85.3°F      ext_diff −1.8  (estamos por DEBAJO)
mercado Kalshi   83-84: 34%   ·   85-86: 42%   ·   87-88: 10%
nuestro our_p    83-84: 88%   ·   85-86:  3%   ·   87-88:  3%
```

⚠ El 0.030 de los bins altos **no es una estimación**: es el piso del
calibrador. El ensemble crudo les da **exactamente cero**.

## La predicción que se puntúa

**El mercado acierta y nosotros no.** Esperamos settle ≥85°F. Si cierra en 83-84
el ensemble tenía razón y esta nota queda como aviso de no fiarse del ojo.

## 🔴 El defecto que este día expone, independientemente del settle

Entre las 15:10 y las 15:36 llegó el METAR de las 14:53 y `max_obs` saltó de
78.1 a 82.0 — casi cuatro grados de información nueva. Qué hizo cada consumidor:

```
techo físico     83.0 → 87.0   ✅ se reajustó
piso             78.08 → 82.04 ✅ se reajustó
ens_med          83.5 → 83.5   🔴 NO se movió
our_p de 85-86   0.032 → 0.030 🔴 BAJÓ
```

Es [[principio_todo_se_reajusta]] fallando en su segunda mitad: el dato entró,
unos consumidores se enteraron y **la predicción no**.

La causa está a la vista en la lectura de las 15:10: `eff_N = 1.5 de 31`. Con el
reweight colapsado sobre un miembro y medio, los pesos ya están saturados y una
observación nueva no puede moverlos. **Un reweight colapsado deja de aprender**,
y hoy `difficulty` lo reporta como informativo sin que nadie actúe.

Y hay una incoherencia interna que no depende del mercado: nuestro **propio**
techo físico dice que el día puede llegar a 87.0 (p90 de subida restante) y
nuestra **propia** distribución no tiene un solo miembro por encima de 84.3. Las
dos salen del mismo snapshot.

## Qué NO es

No es un problema de calibración —el calibrador ya está haciendo lo que puede,
subiendo un cero a 0.030— ni de sesgo de nivel. Es de **dispersión**: la banda
de 0.5°F afirma una certeza que el día no respalda.

## Pendiente

Si el settle confirma ≥85, esto pide una corrida propia y pre-registrada sobre
el colapso del reweight: cada cuánto pasa, y si `eff_N` bajo predice el error
(al contrario que `difficulty`, que ya se midió y **no** lo predice, N=505).

---

## ✅ PUNTUADO el 2026-08-28 — settle **85.0°F** (NWS CLI, `day_outcomes`)

**La predicción acertó, y era contra nosotros mismos.** El settle cae en el bin
85-86, que el mercado tenía al 42% y nuestro `our_p` al 3% — un 3% que además
era el piso del calibrador, porque el ensemble crudo le daba **cero**.

```
mercado    85-86 al 42%   ✅ acertó
nosotros   83-84 al 88%   🔴 falló, con banda de 0.5°F
ens_med    83.5    →  error +1.5°F bajo el settle
techo físico 87.0        (nuestro propio techo SÍ contenía el resultado)
```

El día terminó 3.0°F por encima del miembro más caliente de las 500 corridas.
La incoherencia interna que señalaba la nota queda confirmada por el
termómetro: el techo físico y la distribución del ensemble salen del **mismo
snapshot** y sólo uno de los dos contenía el resultado.

⚠ Un acierto de N=1 no valida el ojo. Lo que sí queda medido es el **defecto**,
que no dependía del settle: `max_obs` saltó 3.9°F y `ens_med` no se movió.

**Consecuencia**: se abre la corrida pre-registrada sobre el colapso del
reweight — pero **no** sobre `eff_N` como predictor del error, que ya se midió
y se descartó (N=164, rho=+0.070, p=0.373, [[backtest_difficulty_componentes]]).
La pregunta viva es la otra mitad: si un reweight colapsado deja de **aprender**
de las observaciones nuevas. Ver la fila del 2026-08-28 en `DECISIONES.md`.
