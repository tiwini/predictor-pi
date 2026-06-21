#!/usr/bin/env bash
# Arranca weather :8000, crypto :8001 y dashboard tras reboot.
# Uso: ./start_all.sh (desde el dir del repo)
set -u

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

log() { echo "[start_all] $*"; }

is_up() { curl -sf -o /dev/null -m 2 "http://127.0.0.1:$1/" ; }

start_weather() {
  if is_up 8000; then log "weather :8000 ya está arriba"; return; fi
  cd "$SCRIPT_DIR/weather-predictor" || { log "no existe weather-predictor"; return; }
  nohup ./venv/bin/python3 predictor_web.py > web.log 2>&1 &
  log "weather :8000 lanzado (PID $!)"
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

start_weather
start_crypto
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
