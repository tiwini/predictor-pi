#!/usr/bin/env bash
# Arranca weather :8000, crypto :8001 y dashboard tras reboot.
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
wait_api_reachable() {
  local url="${1:-https://api.weather.gov/}"
  local max="${2:-120}"
  local i=0
  while ! curl -sf -o /dev/null -m 5 "$url"; do
    i=$((i+5))
    if [ "$i" -ge "$max" ]; then
      log "$url no respondió tras ${max}s — sigo igual (weather puede morir)"
      return 1
    fi
    sleep 5
  done
  log "$url reachable tras ${i}s"
}

# Arranca weather con retry: si :8000 no responde tras 20s, mata proceso
# zombie y reintenta hasta 3 veces con backoff 30s. Cubre el fallo post-reboot
# donde NWS aún no está reachable al primer intento (Jul 5 2026).
start_weather_with_retry() {
  if is_up 8000; then log "weather :8000 ya está arriba"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  local attempt pid i
  for attempt in 1 2 3; do
    wait_api_reachable https://api.weather.gov/ 120
    nohup ./venv/bin/python3 predictor_web.py > web.log 2>&1 &
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
    if [ "$attempt" -lt 3 ]; then
      log "weather :8000 backoff 30s antes del intento $((attempt+1))"
      sleep 30
    fi
  done
  log "weather :8000 FALLÓ tras 3 intentos"
}

start_crypto() {
  if is_up 8001; then log "crypto :8001 ya está arriba"; return; fi
  cd "$SCRIPT_DIR/crypto-predictor" || { log "no existe crypto-predictor"; return; }
  nohup ./venv/bin/python3 predictor_web.py 8001 > web.log 2>&1 &
  log "crypto :8001 lanzado (PID $!)"
}

start_dashboard() {
  if pgrep -f "python.*dashboard\.py" > /dev/null; then log "dashboard ya corre"; return; fi
  cd "$SCRIPT_DIR" || return
  nohup ./weather-predictor/venv/bin/python3 dashboard.py > dashboard.log 2>&1 &
  log "dashboard lanzado (PID $!)"
}

start_analysis_poller() {
  if pgrep -f "python.*analysis_poller\.py" > /dev/null; then log "analysis_poller ya corre"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  nohup ./venv/bin/python3 analysis_poller.py > analysis_poller.log 2>&1 &
  log "analysis_poller lanzado (PID $!)"
}

start_btc_quarter_poller() {
  if pgrep -f "python.*btc_quarter_poller\.py" > /dev/null; then log "btc_quarter_poller ya corre"; return; fi
  cd "$SCRIPT_DIR" || return
  nohup ./weather-predictor/venv/bin/python3 btc_quarter_poller.py > btc_quarter_poller.log 2>&1 &
  log "btc_quarter_poller lanzado (PID $!)"
}

wait_dns api.weather.gov 60
start_weather_with_retry
#start_crypto  # comentado 2026-07-08: crypto migrado a systemd (crypto-predictor.service)
start_dashboard
start_analysis_poller
start_btc_quarter_poller

sleep 2
log "estado:"
is_up 8000 && log "  ✓ weather :8000 responde" || log "  ✗ weather :8000 NO responde"
is_up 8001 && log "  ✓ crypto  :8001 responde" || log "  ✗ crypto  :8001 NO responde"
pgrep -f "python.*dashboard\.py" > /dev/null && log "  ✓ dashboard corre" || log "  ✗ dashboard NO corre"
pgrep -f "python.*analysis_poller\.py" > /dev/null && log "  ✓ analysis_poller corre" || log "  ✗ analysis_poller NO corre"
pgrep -f "python.*btc_quarter_poller\.py" > /dev/null && log "  ✓ btc_quarter_poller corre" || log "  ✗ btc_quarter_poller NO corre"
