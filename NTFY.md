# ntfy — cómo restaurarlo tras una reinstalación

**El topic no está en este repo y no puede estarlo: el repo es público.** En
ntfy el topic es la credencial, y quien lo conoce no sólo lee tus avisos —
puede **publicar** en ellos. Un desconocido mandándote «Poller relanzado» o
«Sin fuente» convierte el canal de averías en el sitio del que menos te fías.

## Dónde vive

    ~/.config/ntfy.env        permisos 600, una línea:  NTFY_TOPIC=<el tuyo>

Fuera del árbol de git. `.gitignore` no hace falta porque ni siquiera está
dentro, pero conviene no moverlo aquí por comodidad.

## Quién lo lee

| mecanismo | lo usan |
|---|---|
| el fichero `~/.config/ntfy.env` | `proceso_watchdog`, `corrector_watchdog`, `brier_watchdog`, `weekly_guard_digest` |
| la variable de entorno `NTFY_TOPIC` | `notify.py` → `weather_alerts`, `overnight` — **apagadas a propósito** (modo selectivo desde 2026-06-28) |

Son dos interruptores distintos. Crear el fichero NO enciende las cuatro
alertas en vivo, que es justo lo que se quiere.

## Restaurar

1. Recupera el topic de donde lo guardaste (gestor de contraseñas). Si lo has
   perdido, genera otro y **vuelve a suscribirlo en el móvil**:

       umask 077
       printf 'NTFY_TOPIC=predictor-pi-%s\n' "$(openssl rand -hex 12)" > ~/.config/ntfy.env
       chmod 600 ~/.config/ntfy.env
       cat ~/.config/ntfy.env     # suscribe este topic en la app

2. Comprueba que llega de verdad. **Un canal no está verificado hasta que se
   ha visto llegar un aviso al móvil**: el 2026-10-18 el código era correcto,
   los tests pasaban y el canal seguía mudo porque los emoji del título no
   caben en una cabecera HTTP (latin-1).

       cd ~/predictor-pi && ./weather-predictor/venv/bin/python3 -c "
       import sys; sys.path.insert(0,'.')
       import proceso_watchdog as W
       print(W.push('Prueba de canal', 'si lees esto, funciona',
                    prioridad='default', tags='white_check_mark'))"

3. `start_all.sh` avisa en `boot.log` si el fichero falta. Si tras un arranque
   lees `✗ FALTA ~/.config/ntfy.env`, los watchdogs están diagnosticando
   correctamente y hablando solos.

## Por qué tanta ceremonia

El patrón que más caro ha salido en este proyecto no es un cálculo mal hecho:
es **una medida correcta que no llega a nadie**. KHOU pidió revertir 10 días
seguidos, KSFO pidió reactivarse 5, y dos apagones (09-22 y 10-10) se
diagnosticaron bien en el log sin que nadie los viera. Las tres veces el
informe tenía razón y el canal estaba mudo.
