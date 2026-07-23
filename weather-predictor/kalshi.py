"""Market fetcher for daily high-temperature contracts (Kalshi).

Las 5 estaciones curadas (KPHX, KLAX, KLAS, KNYC, KBOS) tienen serie KXHIGH
en Kalshi. Liquidación oficial = NWS Climatological Report (CLI) del WFO
correspondiente. Bins B-prefix = 2°F enteros; T-prefix = colas abiertas.
Persistimos bid/ask/mid por poll en market_cache.db.

Nota NY: Kalshi (KXHIGHNY) liquida con NYC CLI (Central Park = KNYC),
mismo id que nuestro station id post-rename 2026-07-22.
"""
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import requests

DB_PATH = Path(__file__).parent / "market_cache.db"
API = "https://api.elections.kalshi.com/trade-api/v2"
UA = "weather-predictor/0.1"

# NWS station id → Kalshi series ticker. Source of truth en stations.py.
from stations import STATION_TO_SERIES  # noqa: E402


@dataclass
class MarketBin:
    ticker: str
    bin_lo: float       # inclusive lower bound of reported whole-°F max
    bin_hi: float       # inclusive upper bound
    label: str          # e.g. "54° to 55°" or "62° or above"
    yes_bid: float | None   # 0.0–1.0
    yes_ask: float | None
    yes_mid: float | None   # (bid+ask)/2 when both present


def series_for(station_id: str) -> str | None:
    """Devuelve la serie Kalshi para station_id, o None si no soportada."""
    return STATION_TO_SERIES.get(station_id.upper())


def event_ticker_for(series: str, target_date: date) -> str:
    """Kalshi per-day event ticker format: SERIES-YYMMMDD (e.g. KXHIGHNY-26APR19)."""
    month_abbr = target_date.strftime("%b").upper()
    return f"{series}-{target_date.strftime('%y')}{month_abbr}{target_date.strftime('%d')}"


def _parse_ticker_bin(ticker: str, label: str) -> tuple[float, float] | None:
    """Return (bin_lo, bin_hi) in °F for a market ticker.

    B-prefix: center°F with ±0.5 half-width interpreted as a 2°F integer bin.
      Ticker "...B54.5" with label "54° to 55°" → (54, 55).
    T-prefix: open-ended threshold.
      "...T54" with label "53° or below" → (-inf, 53).
      "...T61" with label "62° or above" → (62, inf).
    """
    m = re.search(r"-([BT])(\d+(?:\.\d+)?)$", ticker)
    if not m:
        return None
    kind, num_s = m.group(1), m.group(2)
    num = float(num_s)
    if kind == "B":
        # center halfway between two integers, e.g. 54.5 → 54 to 55
        lo = int(num - 0.5)
        hi = int(num + 0.5)
        return (float(lo), float(hi))
    # T: check label to decide side
    lbl = label.lower()
    if "below" in lbl:
        return (float("-inf"), num - 1)
    if "above" in lbl:
        # Kalshi convention: ticker T{N} carries label "N+1° or above" →
        # the strike is num+1, not num. Docstring above intentionally shows
        # (num+1, inf); prior code returned (num, inf) which shifted the
        # hot tail 1°F cold, contaminating settles and Brier.
        return (num + 1, float("inf"))
    return None


def fetch_bins(station_id: str, target_date: date,
               timeout: int = 15) -> list[MarketBin]:
    """Fetch all bin markets for the given station+date desde Kalshi.
    Devuelve [] si la station no está en STATION_TO_SERIES o el evento aún
    no abrió.
    """
    series = series_for(station_id)
    if series is None:
        return []
    ev = event_ticker_for(series, target_date)
    r = requests.get(f"{API}/markets",
                     params={"event_ticker": ev, "limit": 100},
                     headers={"User-Agent": UA},
                     timeout=timeout)
    if r.status_code == 404:
        return []
    r.raise_for_status()
    markets = r.json().get("markets", [])
    out = []
    for m in markets:
        tk = m["ticker"]
        label = m.get("yes_sub_title", "")
        rng = _parse_ticker_bin(tk, label)
        if rng is None:
            continue
        lo, hi = rng
        yb = m.get("yes_bid_dollars")
        ya = m.get("yes_ask_dollars")
        mid = None
        if yb is not None and ya is not None:
            mid = (float(yb) + float(ya)) / 2
        out.append(MarketBin(ticker=tk, bin_lo=lo, bin_hi=hi, label=label,
                             yes_bid=yb, yes_ask=ya, yes_mid=mid))
    # sort by lower bound so order is natural
    out.sort(key=lambda b: b.bin_lo)
    return out


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.executescript("""
        CREATE TABLE IF NOT EXISTS market_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fetched_at TEXT NOT NULL,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            ticker TEXT NOT NULL,
            bin_lo REAL NOT NULL,
            bin_hi REAL NOT NULL,
            label TEXT,
            yes_bid REAL,
            yes_ask REAL,
            yes_mid REAL,
            our_p REAL,
            our_p_final REAL
        );
        CREATE INDEX IF NOT EXISTS idx_mp_station_date
            ON market_prices(station_id, date);
        CREATE INDEX IF NOT EXISTS idx_mp_ticker_time
            ON market_prices(ticker, fetched_at);
    """)
    # Migración idempotente para DBs creadas antes de 2026-06-19.
    try:
        c.execute("ALTER TABLE market_prices ADD COLUMN our_p_final REAL")
    except sqlite3.OperationalError:
        pass
    return c


def our_p_for_bin(ensemble_maxes: list, bin_lo: float, bin_hi: float) -> float:
    """Fraction of ensemble members whose simulated daily max falls in
    [bin_lo, bin_hi] (inclusive). For open tails, use ±inf.

    Laplace smoothing avoids reporting 0.00/1.00 when the ensemble
    concentrates in one bin — those extremes propagate through the
    pipeline as "calibrated" probs and give phantom infinite edges.
    predictor._prepare_ensemble resamples ~31 raw GFS+external members
    to N_SAMPLES=500 via proportional replication (no new info), so we
    anchor the prior strength to the effective sample size ~31.
    Natural range with EFF_N=31: [0.030, 0.970]."""
    if not ensemble_maxes:
        return 0.0
    n = len(ensemble_maxes)
    if bin_lo == float("-inf"):
        lo = float("-inf")
    else:
        lo = bin_lo - 0.5
    if bin_hi == float("inf"):
        hi = float("inf")
    else:
        hi = bin_hi + 0.5
    cnt = sum(1 for v in ensemble_maxes if lo <= v < hi)
    raw_p = cnt / n
    eff_n = min(n, 31)
    return (raw_p * eff_n + 1) / (eff_n + 2)


def record(station_id: str, target_date: date, bins: list[MarketBin],
           ensemble_maxes: list | None = None,
           fetched_at: datetime | None = None,
           our_p_final_per_bin: list | None = None) -> None:
    """Persist one snapshot of market prices + our predicted_p per bin.

    our_p = raw ensemble fraction (Bayesian reweight + bias + posterior shift
    ya aplicados vía ensemble_maxes). our_p_final = misma cantidad + isotonic
    + blend_with_external; refleja exactamente lo que ve el usuario en /edge
    y es lo que Brier histórico debería leer.
    """
    ts = (fetched_at or datetime.utcnow()).isoformat()
    c = _conn()
    for i, b in enumerate(bins):
        our_p = None
        if ensemble_maxes:
            our_p = our_p_for_bin(ensemble_maxes, b.bin_lo, b.bin_hi)
        our_p_final = None
        if our_p_final_per_bin is not None and i < len(our_p_final_per_bin):
            our_p_final = our_p_final_per_bin[i]
        c.execute("""INSERT INTO market_prices
            (fetched_at, station_id, date, ticker, bin_lo, bin_hi,
             label, yes_bid, yes_ask, yes_mid, our_p, our_p_final)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, station_id, target_date.isoformat(), b.ticker,
             b.bin_lo, b.bin_hi, b.label,
             b.yes_bid, b.yes_ask, b.yes_mid, our_p, our_p_final))
    c.commit()
    c.close()


def implied_prob_above(bins: list, threshold: float) -> float | None:
    """Kalshi's implied P(max > threshold) by summing bins above X.

    `bins` is a list of dicts or MarketBin objects with bin_lo, bin_hi, yes_mid.
    Each B-bin covers 2 integer °F values; within-bin we assume uniform.

    Returns None when the threshold falls inside an open tail (T-low or
    T-high) whose mass is non-trivial — we can't split the tail without
    extra assumptions, so we're honest about ignorance rather than guess.
    """
    if not bins:
        return None
    # sort and identify tail endpoints
    sb = sorted(bins, key=lambda b: (b["bin_lo"] if isinstance(b, dict) else b.bin_lo))
    tail_low = None
    tail_high = None
    for b in sb:
        lo = b["bin_lo"] if isinstance(b, dict) else b.bin_lo
        hi = b["bin_hi"] if isinstance(b, dict) else b.bin_hi
        if lo == float("-inf"):
            tail_low = b
        if hi == float("inf"):
            tail_high = b

    # abort if the threshold lies inside (or below) a tail with mass > 2% —
    # can't split the tail distribution cleanly
    THR = 0.02
    if tail_low is not None:
        p = tail_low["yes_mid"] if isinstance(tail_low, dict) else tail_low.yes_mid
        hi = tail_low["bin_hi"] if isinstance(tail_low, dict) else tail_low.bin_hi
        if p is not None and p > THR and threshold < hi:
            return None
    if tail_high is not None:
        p = tail_high["yes_mid"] if isinstance(tail_high, dict) else tail_high.yes_mid
        lo = tail_high["bin_lo"] if isinstance(tail_high, dict) else tail_high.bin_lo
        if p is not None and p > THR and threshold >= lo:
            return None

    total = 0.0
    for b in sb:
        lo = b["bin_lo"] if isinstance(b, dict) else b.bin_lo
        hi = b["bin_hi"] if isinstance(b, dict) else b.bin_hi
        p = b["yes_mid"] if isinstance(b, dict) else b.yes_mid
        if p is None:
            return None
        if lo == float("-inf"):
            # all mass ≤ hi; if threshold ≥ hi no contribution, else tail guard
            # above already handled the tricky case
            continue
        if hi == float("inf"):
            # all mass ≥ lo; threshold < lo → full p (guarded above for inside)
            if threshold < lo:
                total += p
            continue
        n_temps = int(hi - lo + 1)
        n_above = sum(1 for t in range(int(lo), int(hi) + 1) if t > threshold)
        total += p * (n_above / n_temps)
    # Noisy bid/ask can make individual bins slightly negative or
    # sum slightly above 1; clamp the final probability to [0, 1].
    return max(0.0, min(1.0, total))


def reliability(station_id: str | None = None, n_buckets: int = 10,
                outcomes_db: str | None = None) -> dict:
    """Reliability of Kalshi's yes_mid prices.

    Joins market_prices with day_outcomes (from calibration.db by default)
    to get the actual daily max, computes per-bin outcome, buckets by
    yes_mid, returns hit rate per bucket + Brier score.
    """
    if outcomes_db is None:
        outcomes_db = str(DB_PATH.parent / "calibration.db")
    c = sqlite3.connect(DB_PATH)
    c.execute(f"ATTACH DATABASE '{outcomes_db}' AS cal")
    # Join: market price snapshot + actual max for that station+date
    # Outcome: 1 if bin contains actual max (integer rounded), else 0
    if station_id:
        rows = c.execute("""
            SELECT mp.yes_mid, mp.bin_lo, mp.bin_hi, d.max_obs_f
            FROM market_prices mp
            JOIN cal.day_outcomes d
              ON d.station_id = mp.station_id AND d.date = mp.date
            WHERE mp.yes_mid IS NOT NULL AND mp.station_id=?
        """, (station_id,)).fetchall()
        total = c.execute("""SELECT COUNT(*) FROM market_prices
                             WHERE station_id=?""", (station_id,)).fetchone()[0]
    else:
        rows = c.execute("""
            SELECT mp.yes_mid, mp.bin_lo, mp.bin_hi, d.max_obs_f
            FROM market_prices mp
            JOIN cal.day_outcomes d
              ON d.station_id = mp.station_id AND d.date = mp.date
            WHERE mp.yes_mid IS NOT NULL
        """).fetchall()
        total = c.execute("SELECT COUNT(*) FROM market_prices").fetchone()[0]
    c.close()

    # Compute (pred, outcome) pairs
    pairs = []
    for yes_mid, lo, hi, mx in rows:
        mx_int = round(mx)
        if lo == float("-inf") or lo == -float("inf"):
            outcome = 1 if mx_int <= hi else 0
        elif hi == float("inf"):
            outcome = 1 if mx_int >= lo else 0
        else:
            outcome = 1 if lo <= mx_int <= hi else 0
        pairs.append((yes_mid, outcome))

    # Bucketize
    buckets = []
    width = 1.0 / n_buckets
    for i in range(n_buckets):
        low = i * width
        high = low + width
        in_b = [(p, o) for p, o in pairs
                if (low <= p < high) or (i == n_buckets - 1 and p == 1.0)]
        if not in_b:
            buckets.append({"low": low, "high": high, "n": 0,
                            "mean_pred": 0.0, "hit_rate": 0.0})
            continue
        n = len(in_b)
        mean_pred = sum(p for p, _ in in_b) / n
        hit_rate = sum(o for _, o in in_b) / n
        buckets.append({"low": low, "high": high, "n": n,
                        "mean_pred": mean_pred, "hit_rate": hit_rate})

    brier = (sum((p - o) ** 2 for p, o in pairs) / len(pairs)) if pairs else None
    return {
        "buckets": buckets,
        "total_n": total,
        "settled_n": len(pairs),
        "brier": brier,
        "station_id": station_id,
    }


def edge_analysis(station_id: str | None = None,
                  outcomes_db: str | None = None,
                  bucket_edges: list[float] | None = None) -> dict:
    """Performance histórica por bucket de edge (our_p - yes_mid).

    Por cada bucket devuelve n, mean_edge, hit_rate (outcome=1 si el bin
    contuvo el max observado), y un ROI hipotético: pagas yes_mid por YES
    cuando edge>0 (o 1-yes_mid por NO cuando edge<0), recibes 1 si aciertas.
    """
    if bucket_edges is None:
        bucket_edges = [-1.0, -0.20, -0.10, -0.05, -0.02,
                        0.02, 0.05, 0.10, 0.20, 1.0]
    if outcomes_db is None:
        outcomes_db = str(DB_PATH.parent / "calibration.db")
    c = sqlite3.connect(DB_PATH)
    c.execute(f"ATTACH DATABASE '{outcomes_db}' AS cal")
    params: tuple = ()
    where = "mp.yes_mid IS NOT NULL AND mp.our_p IS NOT NULL"
    if station_id:
        where += " AND mp.station_id=?"
        params = (station_id,)
    rows = c.execute(f"""
        SELECT mp.yes_mid, mp.our_p, mp.bin_lo, mp.bin_hi, d.max_obs_f
        FROM market_prices mp
        JOIN cal.day_outcomes d
          ON d.station_id = mp.station_id AND d.date = mp.date
        WHERE {where}
    """, params).fetchall()
    c.close()

    pairs = []
    for yes_mid, our_p, lo, hi, mx in rows:
        mx_int = round(mx)
        if lo == float("-inf"):
            outcome = 1 if mx_int <= hi else 0
        elif hi == float("inf"):
            outcome = 1 if mx_int >= lo else 0
        else:
            outcome = 1 if lo <= mx_int <= hi else 0
        edge = our_p - yes_mid
        pairs.append((edge, yes_mid, our_p, outcome))

    buckets = []
    for i in range(len(bucket_edges) - 1):
        low, high = bucket_edges[i], bucket_edges[i + 1]
        in_b = [(e, ym, op, o) for e, ym, op, o in pairs
                if low <= e < high or (i == len(bucket_edges) - 2 and e == high)]
        if not in_b:
            buckets.append({"low": low, "high": high, "n": 0,
                            "mean_edge": 0.0, "hit_rate": 0.0, "roi": 0.0})
            continue
        n = len(in_b)
        mean_edge = sum(e for e, *_ in in_b) / n
        hit_rate = sum(o for *_, o in in_b) / n
        # ROI: buy YES (cost=yes_mid, payout=1) when edge>0;
        #      buy NO (cost=1-yes_mid, payout=1) when edge<0
        total_cost = 0.0
        total_payout = 0.0
        for e, ym, _op, o in in_b:
            if e >= 0:
                total_cost += ym
                total_payout += o
            else:
                total_cost += 1 - ym
                total_payout += 1 - o
        roi = (total_payout - total_cost) / total_cost if total_cost > 0 else 0.0
        buckets.append({"low": low, "high": high, "n": n,
                        "mean_edge": mean_edge, "hit_rate": hit_rate,
                        "roi": roi})
    return {
        "buckets": buckets,
        "settled_n": len(pairs),
        "station_id": station_id,
    }


def current_edges(station_id: str, target_date: date,
                  min_abs_edge: float = 0.05) -> list[dict]:
    """Lista de bins con |our_p - yes_mid| >= min_abs_edge, ordenada por |edge| desc."""
    rows = latest_snapshot(station_id, target_date)
    out = []
    for r in rows:
        if r["our_p"] is None or r["yes_mid"] is None:
            continue
        edge = r["our_p"] - r["yes_mid"]
        if abs(edge) < min_abs_edge:
            continue
        r2 = dict(r)
        r2["edge"] = edge
        out.append(r2)
    out.sort(key=lambda r: -abs(r["edge"]))
    return out


def movement_history(station_id: str, target_date: date) -> dict:
    """Series temporales por ticker: lista de (fetched_at, yes_mid, our_p).

    Devuelve {
      'bins': [{ticker, label, bin_lo, bin_hi, points: [{t, yes_mid, our_p}]}],
      'span': (first_fetched_at, last_fetched_at),
    }
    """
    c = _conn()
    cur = c.execute("""
        SELECT ticker, bin_lo, bin_hi, label, fetched_at, yes_mid, our_p
        FROM market_prices
        WHERE station_id=? AND date=?
        ORDER BY ticker, fetched_at
    """, (station_id, target_date.isoformat()))
    by_ticker = {}
    first = None
    last = None
    for ticker, lo, hi, label, ts, ym, op in cur.fetchall():
        if ticker not in by_ticker:
            by_ticker[ticker] = {
                "ticker": ticker, "bin_lo": lo, "bin_hi": hi,
                "label": label, "points": [],
            }
        by_ticker[ticker]["points"].append(
            {"t": ts, "yes_mid": ym, "our_p": op}
        )
        if first is None or ts < first:
            first = ts
        if last is None or ts > last:
            last = ts
    c.close()
    bins = sorted(by_ticker.values(), key=lambda b: b["bin_lo"])
    return {"bins": bins, "span": (first, last)}


def latest_snapshot(station_id: str, target_date: date) -> list[dict]:
    """Return the most recent row per ticker for (station, date).

    Each dict has: ticker, bin_lo, bin_hi, label, yes_bid, yes_ask, yes_mid,
    our_p, fetched_at.
    """
    c = _conn()
    cur = c.execute("""
        SELECT ticker, bin_lo, bin_hi, label, yes_bid, yes_ask, yes_mid,
               our_p, fetched_at
        FROM market_prices
        WHERE station_id=? AND date=?
          AND fetched_at = (SELECT MAX(fetched_at) FROM market_prices
                            WHERE station_id=? AND date=? AND ticker=market_prices.ticker)
        ORDER BY bin_lo
    """, (station_id, target_date.isoformat(), station_id, target_date.isoformat()))
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    c.close()
    return rows
