#!/usr/bin/env bash
# Arranca weather :8000 (con el panel montado en /panel) y crypto :8001
# tras reboot. El dashboard en :8080 se retiro el 2026-08-21.
# Uso: ./start_all.sh (desde el dir del repo)
set -u

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

log() { echo "[start_all] $*"; }

is_up() { curl -sf -o /dev/null -m 8 "http://127.0.0.1:$1/" ; }

# Espera resolución DNS (rápido, solo pre-check antes del reachability real).
wait_dns() {
  local host="${1:-api.weather.gov}"
  local max="${2:-60}"
  local i=0
  while ! getent hosts "$host" >/dev/null 2>&1; do
    i=$((i+1))
    if [ "$i" -ge "$max" ]; then
      log "DNS para $host nunca resolvió tras ${max}s — sigo igual"
      return 1
    fi
    sleep 1
  done
  log "DNS para $host listo tras ${i}s"
}

# Espera reachability REAL vía HTTPS GET. Distinto de wait_dns: cachés DNS
# pueden retornar sin que el host responda. Esta es la barrera antes de
# lanzar predictor_web (que muere al primer request si NWS no está listo).
#
# 2026-08-09: el tope era 120s y al volver de un apagón (08-08) la red tardó
# más. Los 3 intentos se agotaron en ~8 min y predictor_web quedó caído 12,5 h
# mientras el poller sí funcionaba. Ahora espera hasta 30 min con sondeo
# espaciado — un @reboot en background puede permitírselo, y quedarse sin web
# medio día no.
wait_api_reachable() {
  local url="${1:-https://api.weather.gov/}"
  local max="${2:-1800}"
  local waited=0 gap=5
  while ! curl -sf -o /dev/null -m 10 "$url"; do
    if [ "$waited" -ge "$max" ]; then
      log "$url no respondió tras ${waited}s — abandono la espera"
      return 1
    fi
    sleep "$gap"
    waited=$((waited+gap))
    [ "$gap" -lt 60 ] && gap=$((gap*2))
  done
  log "$url reachable tras ${waited}s"
}

# Arranca weather con retry: si :8000 no responde tras 20s, mata proceso
# zombie y reintenta con backoff CRECIENTE. Cubre el fallo post-reboot donde
# NWS aún no está reachable al primer intento (Jul 5 2026) y el del apagón
# (Ago 8 2026), donde el DNS tardó más que toda la secuencia de reintentos.
#
# Cambio clave: si la API no responde NO se lanza el proceso. Antes se lanzaba
# igual ("sigo igual, weather puede morir") y cada intento se quemaba contra
# una red que aún no estaba, agotando los 3 en minutos.
start_weather_with_retry() {
  if is_up 8000; then log "weather :8000 ya está arriba"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  local attempt pid i backoff=30
  for attempt in 1 2 3 4 5; do
    if ! wait_api_reachable https://api.weather.gov/ 1800; then
      log "weather :8000 intento $attempt: API inalcanzable, no lanzo"
      if [ "$attempt" -lt 5 ]; then
        log "weather :8000 backoff ${backoff}s antes del intento $((attempt+1))"
        sleep "$backoff"; backoff=$((backoff*2))
      fi
      continue
    fi
    nohup ./venv/bin/python3 predictor_web.py >> web.log 2>&1 &
    pid=$!
    log "weather :8000 intento $attempt (PID $pid)"
    # Espera hasta 20s por respuesta HTTP local
    for i in 1 2 3 4 5 6 7 8 9 10; do
      sleep 2
      if is_up 8000; then
        log "weather :8000 ARRIBA en intento $attempt (${i}·2s)"
        return
      fi
      if ! kill -0 "$pid" 2>/dev/null; then
        log "weather :8000 murió en intento $attempt (ver web.log)"
        break
      fi
    done
    # Si sigue vivo pero no responde, matarlo antes de re-intentar
    kill "$pid" 2>/dev/null && log "weather :8000 zombie killed (intento $attempt)"
    if [ "$attempt" -lt 5 ]; then
      log "weather :8000 backoff ${backoff}s antes del intento $((attempt+1))"
      sleep "$backoff"; backoff=$((backoff*2))
    fi
  done
  log "weather :8000 FALLÓ tras 5 intentos"
}

start_crypto() {
  if is_up 8001; then log "crypto :8001 ya está arriba"; return; fi
  cd "$SCRIPT_DIR/crypto-predictor" || { log "no existe crypto-predictor"; return; }
  nohup ./venv/bin/python3 predictor_web.py 8001 >> web.log 2>&1 &
  log "crypto :8001 lanzado (PID $!)"
}

start_dashboard() {
  if pgrep -f "python.*dashboard\.py" > /dev/null; then log "dashboard ya corre"; return; fi
  cd "$SCRIPT_DIR" || return
  nohup ./weather-predictor/venv/bin/python3 dashboard.py >> dashboard.log 2>&1 &
  log "dashboard lanzado (PID $!)"
}

start_analysis_poller() {
  if pgrep -f "python.*analysis_poller\.py" > /dev/null; then log "analysis_poller ya corre"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  nohup ./venv/bin/python3 analysis_poller.py >> analysis_poller.log 2>&1 &
  log "analysis_poller lanzado (PID $!)"
}

start_kalshi_fast_poller() {
  if pgrep -f "python.*kalshi_fast_poller\.py" > /dev/null; then log "kalshi_fast_poller ya corre"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  nohup ./venv/bin/python3 kalshi_fast_poller.py >> kalshi_fast_poller.log 2>&1 &
  log "kalshi_fast_poller lanzado (PID $!)"
}

start_btc_quarter_poller() {
  if pgrep -f "python.*btc_quarter_poller\.py" > /dev/null; then log "btc_quarter_poller ya corre"; return; fi
  cd "$SCRIPT_DIR" || return
  nohup ./weather-predictor/venv/bin/python3 btc_quarter_poller.py >> btc_quarter_poller.log 2>&1 &
  log "btc_quarter_poller lanzado (PID $!)"
}

wait_dns api.weather.gov 300
start_weather_with_retry
#start_crypto  # comentado 2026-07-08: crypto migrado a systemd (crypto-predictor.service)
# start_dashboard  # apagado 2026-08-21: /analysis, /ai y /btc-quarter
#                 se sirven desde :8000/panel (predictor_web._montar_panel).
#                 dashboard.py se conserva porque ES el codigo que sirve
#                 esas paginas; lo que se retira es el segundo puerto.
start_analysis_poller
start_btc_quarter_poller
start_kalshi_fast_poller

sleep 2
log "estado:"
is_up 8000 && log "  ✓ weather :8000 responde" || log "  ✗ weather :8000 NO responde"
is_up 8001 && log "  ✓ crypto  :8001 responde" || log "  ✗ crypto  :8001 NO responde"
pgrep -f "python.*analysis_poller\.py" > /dev/null && log "  ✓ analysis_poller corre" || log "  ✗ analysis_poller NO corre"
pgrep -f "python.*btc_quarter_poller\.py" > /dev/null && log "  ✓ btc_quarter_poller corre" || log "  ✗ btc_quarter_poller NO corre"
pgrep -f "python.*kalshi_fast_poller\.py" > /dev/null && log "  ✓ kalshi_fast_poller corre" || log "  ✗ kalshi_fast_poller NO corre"
