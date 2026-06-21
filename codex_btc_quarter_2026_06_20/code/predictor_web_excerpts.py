"""Relevant excerpts from crypto-predictor/predictor_web.py

These are the three pieces the new quarter-streak feature depends on:

1. /api/quarter-signal — the new endpoint the poller calls
2. _build_intra15 — log-Student-t for "P(close ≥ strike) in next 15-min closes"
3. _compute_tension — 6-signal aggregate score in [-5, +5]

Score components and their weights:
- OB top20 imbalance         → [-1, +1]   (orderbook depth ratio, ~live)
- Taker flow (5m window)     → [-1, +1]   (buy/sell taker volume ratio)
- Funding rate (8h epoch)    → [-0.5, +0.5] (Binance perp funding, slow)
- Fear & Greed index (daily) → [-1, +1]   (contrarian: extreme fear = buy)
- vs BRTI mid (basis bps)    → [-0.5, +0.5] (Binance vs CFB)
- +60min momentum vs band    → [-1.5, +1.5] (extrapolated 1h price vs p10-p90 cone)

Direction labels:
  score ≥ +1.5 → bullish
  score ≥ +0.5 → lean bull
  |score| < 0.5 → neutral
  score ≤ -0.5 → lean bear
  score ≤ -1.5 → bearish
"""

# ──────────────────────────────────────────────────────────────────────
# NEW endpoint added 2026-06-20
# ──────────────────────────────────────────────────────────────────────

@app.route("/api/quarter-signal")
def api_quarter_signal():
    """JSON minimal para el quarter-streak tracker del dashboard.

    Devuelve precio actual + score de tensión [-5, +5] + P(close ≥ now)
    en el próximo cierre de 15 min. El dashboard usa el signo de
    `tension_score` para predecir UP/DOWN cada xx:00/15/30/45.
    """
    with _state_lock:
        snap = dict(_state["BTCUSDT"])
    pred = snap.get("pred")
    if pred is None:
        return jsonify({"error": "sin datos aún"}), 503
    with _external_lock:
        external = dict(_external)
    momentum = snap.get("momentum_pct_per_min")
    momentum_multi = snap.get("momentum_multi") or {}
    momentum_tf = _build_momentum_tf(momentum_multi)
    horizons = _build_horizons(pred, momentum)
    tension = _compute_tension(pred, external, horizons, momentum_tf)
    intra15 = _build_intra15(pred, pred.now_price, n_cierres=1)
    p_above_next = None
    next_close_label = None
    if intra15 and intra15.get("rows"):
        p_above_next = intra15["rows"][0]["p_above"]
        next_close_label = intra15["rows"][0]["label"]
    return jsonify({
        "price": pred.now_price,
        "tension_score": tension["score"] if tension else None,
        "tension_direction": tension["direction"] if tension else None,
        "p_above_next": p_above_next,
        "next_close_label": next_close_label,
        "fetched_at": snap["fetched_at"].isoformat() if snap["fetched_at"] else None,
    })


# ──────────────────────────────────────────────────────────────────────
# Pre-existing helper used by the endpoint (lines ~2144-2197 of predictor_web.py)
# ──────────────────────────────────────────────────────────────────────

def _build_intra15(pred, strike: float | None,
                   n_cierres: int = 4,
                   brti_mid: float | None = None,
                   brti_meta: dict | None = None) -> dict | None:
    """Tabla P(close ≥ strike) en los próximos `n_cierres` cierres de 15 min
    (XX:00/15/30/45). Para mercados Kalshi 15-min BTC. Sin momentum; pura
    distribución log-Student-t escalada con √(min al target).
    """
    import math
    if strike is None or strike <= 0:
        return None
    if pred.sigma_1m <= 0 or pred.now_price <= 0:
        return None
    base_ts = pred.fetched_at
    base_dt = datetime.fromtimestamp(base_ts, tz=timezone.utc)
    next_min = ((base_dt.minute // 15) + 1) * 15
    add_h, m = divmod(next_min, 60)
    first_dt = base_dt.replace(minute=0, second=0, microsecond=0)
    first_dt = first_dt + timedelta(hours=add_h, minutes=m)
    first_unix = first_dt.timestamp()
    rows = []
    for k in range(n_cierres):
        t_unix = first_unix + k * 15 * 60
        mins = max(0.5, (t_unix - base_ts) / 60.0)
        sigma_h = pred.sigma_1m * math.sqrt(mins)
        z = math.log(strike / pred.now_price) / sigma_h
        p_above = 1.0 - _pred._dist_cdf(z)
        rows.append({
            "label": _pr(t_unix).strftime("%H:%M"),
            "mins": mins,
            "sigma_pct": sigma_h * 100.0,
            "p_above": p_above,
            "p_below": 1.0 - p_above,
        })
    out = {"strike": strike, "now_price": pred.now_price, "rows": rows}
    # ... (BRTI adjust omitted for brevity)
    return out


# ──────────────────────────────────────────────────────────────────────
# Pre-existing tension aggregator (lines ~2200-2273 of predictor_web.py)
# ──────────────────────────────────────────────────────────────────────

def _compute_tension(pred, external, horizons, momentum_tf) -> dict | None:
    """Agrega 6 señales en un score direccional [-5, +5]. Solo BTC.

    Cada componente normalizado a [-X, +X]; suma da el score global. Útil
    para leer balance bullish/bearish sin parsear cada pill por separado."""
    if pred.symbol != "BTCUSDT" or not external:
        return None
    components: list[dict] = []
    score = 0.0

    ob = external.get("ob_imbalance")
    if ob:
        c = (ob["imbalance"] - 0.5) * 2.0  # [-1, +1]
        components.append({"k": "OB top20", "c": c,
                           "v": f"{ob['imbalance']*100:.0f}/{(1-ob['imbalance'])*100:.0f}"})
        score += c

    flow = external.get("taker_flow")
    if flow:
        c = (flow["buy_ratio"] - 0.5) * 2.0  # [-1, +1]
        components.append({"k": f"flow {flow['window_min']}m", "c": c,
                           "v": f"{flow['buy_ratio']*100:.0f}/{(1-flow['buy_ratio'])*100:.0f}"})
        score += c

    funding = external.get("funding")
    if funding:
        rate_bps = funding["rate"] * 10000
        c = max(-0.5, min(0.5, rate_bps / 20.0))  # 10bps → ±0.5
        components.append({"k": "funding", "c": c,
                           "v": f"{rate_bps:+.1f}bps/8h"})
        score += c

    fng = external.get("fng")
    if fng:
        v = fng["value"]
        if v <= 25:
            c = 0.5 + (25 - v) / 50.0  # [+0.5, +1] contrarian buy
        elif v >= 75:
            c = -0.5 - (v - 75) / 50.0  # [-1, -0.5] contrarian sell
        else:
            c = 0.0
        components.append({"k": "F&G", "c": c, "v": f"{v}"})
        score += c

    cb_mid = external.get("brti_mid")
    if cb_mid and pred.now_price > 0:
        bps = (pred.now_price / cb_mid - 1) * 10000
        c = max(-0.5, min(0.5, bps / 20.0))  # 10bps → ±0.5
        components.append({"k": "vs BRTI", "c": c, "v": f"{bps:+.1f}bps"})
        score += c

    h60 = next((h for h in horizons if h["h_min"] == 60), None)
    if h60 and h60.get("mom_price"):
        mp = h60["mom_price"]
        p10, p25, p75, p90 = h60["p10"], h60["p25"], h60["p75"], h60["p90"]
        if mp > p90: c = 1.5
        elif mp > p75: c = 0.7
        elif mp < p10: c = -1.5
        elif mp < p25: c = -0.7
        else: c = 0.0
        if c != 0:
            arrow = "↑" if c > 0 else "↓"
            components.append({"k": "+60min", "c": c, "v": f"{arrow}{abs(c):.1f}"})
            score += c

    score = max(-5.0, min(5.0, score))
    if score >= 1.5: direction = "bullish"
    elif score <= -1.5: direction = "bearish"
    elif score >= 0.5: direction = "lean bull"
    elif score <= -0.5: direction = "lean bear"
    else: direction = "neutral"
    pct = (score + 5.0) / 10.0 * 100.0
    return {"score": score, "direction": direction, "pct": pct,
            "components": components}
