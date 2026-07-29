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
