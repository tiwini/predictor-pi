# Pre-registro — ¿el mid pegado es opinión o libro de un solo lado? (`kalshi_fast`, 2026-10-31)

Escrito **antes de ejecutar una sola consulta de precios** sobre `kalshi_fast`.
Lo único mirado hasta aquí es el inventario de la tabla —1.496.880 filas, 74
días (2026-08-20 → 2026-10-31), 20 estaciones, columnas `id ts station ticker
bin_lo bin_hi yes_bid yes_ask yes_mid`— y que la purga de
`analysis_poller.py:570-571` no la toca. Ni un precio, ni una proporción.

Es la pregunta **3** de las cinco que quedaron escritas el 2026-08-20 al montar
el colector rápido. Entonces se anotó «con ~10 días hay N para (1) y (3)»; hay
74.

## La pregunta

Cuando `yes_mid` es muy bajo, ¿hay alguien al otro lado del libro o no? El
ejemplo que quedó sin comprobar aquel día: KPHX bin 111-112 con
`bid 0.00 · ask 0.01 · mid 0.005`. Ese 0.5% se lee como «el mercado le da media
probabilidad» y puede no ser una creencia de nadie.

## Por qué importa

El edge se calcula contra el **mid**. Hoy mismo el monitor reportó *«+81pp:
Model 0.81 vs Kalshi 0.01 = mispricing extremo»*. Si ese precio sale de un libro
con `bid = 0`, el mid no es la opinión del mercado sino el punto medio de un
libro de un solo lado, y está **por debajo del único precio ejecutable** (el
ask). El edge contra mid queda inflado por construcción, y no por un error de
Kalshi.

Esto conecta con tres cosas ya medidas: que los edges no son estructurales (el
Brier de Kalshi nos gana 7/9), que el mercado gana 20 de 20 como elector de
bins, y con el `mid-YES` que sigue pendiente de la revisión del 08-11 con un
control nuevo.

## Qué se mide, exactamente

Universo: filas con `yes_bid IS NOT NULL AND yes_ask IS NOT NULL`.

- `mid_bajo` := `yes_mid <= 0.02`
- `un_solo_lado` := `yes_bid = 0`
- **Métrica principal:** fracción de `mid_bajo` que es `un_solo_lado`.
- **Espejo por arriba:** `yes_mid >= 0.98` con `yes_ask >= 1.00`. Va incluido
  porque la regla «≥0.95 en Kalshi no falla nunca» (N=2209, 100%) se apoya
  justo en precios de ese extremo, y si allí también falta un lado, la regla
  describe otra cosa de la que creemos.
- Desglose por estación y por hora local, sólo para describir.

**Dos denominadores, y la decisión se toma con el segundo:**

1. por fila — 1.496.880, pero con cadencia de 2 min el mismo estado se cuenta
   ~30 veces por hora;
2. por **(día, estación, bin)** única, contando la tripleta como de un solo
   lado si lo estuvo en la **mediana** de sus observaciones.

El pool cruzado exagera siempre: 2 de 3 backtests habrían dado falso positivo
decidiendo con el denominador grande.

## Criterios de decisión, fijados ahora

| resultado (por tripleta) | lectura | acción |
|---|---|---|
| **≥ 60%** | el mid bajo es, por defecto, libro de un solo lado | marcar «sin bid» en la lectura por estación y en el agente; dejar de reportar esos bins como mispricing |
| **30 – 60%** | mixto, hay que mirar caso por caso | exponer `bid`/`ask` donde hoy sólo se ve el mid; no excluir nada automáticamente |
| **< 30%** | la sospecha del 08-20 queda refutada | no se cambia nada, y se anota para no volver a plantearla |

## Qué NO vale

- Decidir con el conteo **por fila**. Se reportan los dos, se decide con la
  tripleta.
- Tratar `yes_mid` nulo como 0.
- Suponer que todas las filas están dentro de la ventana de pico ±1 h porque el
  poller debería guardar sólo eso: se **verifica** con la hora local y, si hay
  filas de fuera, se reportan aparte.
- Y el límite de la conclusión: `bid = 0` con `ask = 0.01` **no** significa «no
  se puede operar». Comprar YES a 1¢ es ejecutable; lo que no existe es con
  quién **salir** antes del settle. La conclusión no puede decir más que eso.

## ✅ Cerrado el 2026-10-31 — 97,2%, la casilla más extrema del criterio

```
POR FILA        mid <= 0.02 → 65,6% de todo el libro · de ellos 95,2% sin bid
POR TRIPLETA    8.849 (día,estación,bin) → 97,2% sin bid en la mediana
ESPEJO ARRIBA   mid >= 0.98 → 92,1% sin ASK (nadie vende)
el ask sin bid  0.01 en 911.493 de 935.208 casos (97,5%)
por estación    uniforme: de 87,0% (KSFO) a 99,8% (KDFW)
cobertura       74 días, 2026-08-20 → 2026-11-01, 1.497.438 filas con bid y ask
```

Los dos denominadores coinciden y **el pool no exageró**: por fila 95,2%, por
tripleta 97,2%, o sea que esta vez el denominador conservador apuntaba más
fuerte. No es una estación ni un día: las veinte pasan del 87%.

Verificación pre-registrada de la ventana: las filas van de **11h a 19h local**,
concentradas 13-17h. Ninguna de fuera.

**Lo que dice:** el «+81pp de mispricing extremo» que reporta el monitor no es
un error del mercado. Es que **nadie compra ese bin** y el ask está en el tick
mínimo. Dos tercios del libro que registramos son bins con mid ≤ 0.02, y de
ellos prácticamente ninguno tiene comprador.

**Lo que NO dice**, y el pre-registro lo dejó escrito para no pasarse de aquí:
`bid = 0` con `ask = 0.01` **sí** se puede comprar. Lo que no existe es con
quién **salir** antes del settle.

**El espejo importa aparte:** 92,1% de los mid ≥ 0.98 no tienen ask. La regla
«≥0.95 en Kalshi no falla nunca» (N=2209, 100%) sigue siendo verdad como
descripción del **resultado**, pero a esos precios no hay quien venda, así que
no describe una entrada disponible. Es una regla sobre lo que va a pasar, no
sobre lo que se puede comprar.

**Acción que fijó el criterio** —marcar «sin bid» y dejar de reportar esos bins
como mispricing— con un obstáculo que el pre-registro no anticipó:
`kalshi_snapshots`, la tabla que alimenta los edges y los backtests, **no
guarda `yes_bid` ni `yes_ask`**: sólo `yes_mid`, `our_p` y `our_p_calibrated`.
En vivo el dato existe (`kalshi.py`, `bets.py`, `kalshi_fast`); en la serie con
la que se decide, no.
