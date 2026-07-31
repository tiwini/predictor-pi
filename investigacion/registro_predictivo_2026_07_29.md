# Registro predictivo — 2026-07-29 · KATL

Anotado el mismo día, **antes del settle**, para cerrarlo mañana. El objetivo no
es acertar: es acumular casos que digan si mi método empírico aporta algo o sólo
añade ruido con aire de rigor.

## Lo que dije a las 15:03 EDT

```
base 84.9 · pendiente última hora +0.0°F/h -> PLANO · 9 días comparables
subida restante CONDICIONADA  +1.6°F  ->  settle ~86.5   (rango 85-89)

para contraste, el mismo momento:
  externos        90.7
  mercado         90-91 con 0.46
  nuestro modelo  92.6
```

Dije que el empírico apuntaba a `87 or below` / `88-89` como infravalorados, con
la cautela de que N=9 y había `regime_break` activo. No lo llamé como apuesta.

## Lo que se veía a las 17:01, con la ventana cerrando

```
CLI parcial (16:39)   88.0     <- piso duro, ya por encima de mi 86.5
feed actual           90°F, 56 min estable
mercado               90-91 a 0.66 · 88-89 a 0.29
nuestro modelo        94.7  (ext_diff +4.0, banda del 92%)
```

**Mi empírico ya está refutado por el CLI**: dije 86.5 y el piso duro es 88.0.
Atlanta subió de 84.9 a 90 en las dos horas posteriores a mi lectura — justo lo
que descarté por la pendiente plana.

## PREDICCIÓN a cerrar mañana

**El settle será ≥90**, o sea el mercado (`90-91` a 0.66) acierta y mi empírico
falla por ~4°F.

Si sale 88-89, mi empírico habría estado más cerca de lo que parece ahora y el
mercado equivocado.

## Balance del método empírico, para evaluarlo con los tres casos

```
2026-07-28  KDFW  predije 102-103   settle 100.0   FALLO (demasiado caliente)
2026-07-28  KATL  predije  97-98    settle  94.0   FALLO (demasiado caliente)
2026-07-29  KATL  predije ~86.5     settle    ?    va camino de FALLO (frío)
```

**El error no tiene signo consistente**: dos días demasiado caliente, uno
demasiado frío. Eso apunta a varianza de un método con N entre 9 y 27, no a un
sesgo corregible. La condicional por pendiente
(`investigacion/subida_condicionada.py`) arregla el modo de fallo del 28 pero no
le da una precisión que no tiene.

**Criterio para mañana**: si el tercer caso también falla, el método empírico no
debería usarse para llamar bins concretos — sólo como contraste de orden de
magnitud frente al modelo, que es el único uso donde ha acertado
consistentemente (las tres veces señalé que nuestro modelo era el menos creíble
de los cuatro, y las tres veces lo fue).

## Verificación

```bash
ssh popeye@100.83.162.24
cd ~/predictor-pi/weather-predictor && ./venv/bin/python3 -c "
import sys; sys.path.insert(0,'.')
from datetime import date
import nws_cli
print('KATL', nws_cli.fetch_max_min_for('KATL', date(2026,7,29)))"
```

---

# RESULTADO (verificado 2026-07-31)

**KATL settle 89.0** (CF6 y day_outcomes coinciden).

```
mi empírico (15:03)      86.5    error -2.5    -> bin 86-87   FALLO
mi predicción escrita    ">=90"                               FALLO
mercado 90-91 a 0.66             error +1.5    -> bin 90-91   FALLO
nuestro modelo (17:01)   94.7    error +5.7    -> bin 94-95   FALLO
CLI parcial (16:39)      88.0    se quedó 1°F corto (nunca sobreestima)
feed 5-min (17:12)       89.6    -> redondea 90, pero el settle fue 89
```

Ganó `88-89`, que cotizaba a **0.29**. No lo tenía nadie.

## Balance final del método empírico: 0 de 3

```
07-28 KDFW  102-103  ->  100.0   FALLO (caliente)
07-28 KATL   97-98   ->   94.0   FALLO (caliente)
07-29 KATL   ~86.5   ->   89.0   FALLO (frío)
```

**Se aplica el criterio escrito de antemano: el empírico NO se usa para llamar
bins concretos.** Tres de tres, con el error cambiando de signo — varianza de un
método con N entre 9 y 27, no un sesgo corregible.

## Lo que SÍ sobrevive, 3 de 3

Las tres veces dije que **nuestro modelo era el menos creíble de los
estimadores**, y las tres veces lo fue:

```
07-28 KATL  modelo pedía 97-98    settle 94.0    error del modelo +3.5
07-28 KDFW  modelo pedía 102.1    settle 100.0   error +2.1
07-29 KATL  modelo pedía 94.7     settle  89.0   error +5.7
```

El uso válido del empírico es **detectar cuándo el modelo se está yendo**, no
elegir bin. Son cosas distintas: la primera sólo pide orden de magnitud, la
segunda pide precisión de 1-2°F que el método no tiene con este N.

## Segunda lección, del mismo día

El mercado también falló, y por más margen que en los otros dos casos. Su
favorito (`90-91` a 0.66) perdió contra un bin de 0.29. Que Kalshi gane el Brier
7 de 9 no lo hace infalible en un día concreto — sólo mejor en agregado.
