import isotonic


def test_empty_samples_returns_none():
    assert isotonic.fit([]) is None


def test_monotone_input_preserved():
    samples = [(0.1, 0), (0.3, 0), (0.5, 1), (0.7, 1), (0.9, 1)]
    cal = isotonic.fit(samples)
    ys = [b.y for b in cal.blocks]
    assert ys == sorted(ys)


def test_violators_pooled():
    # p=0.3 never happens, p=0.7 happens 3x — PAV must pool violations.
    samples = [(0.3, 1), (0.3, 1), (0.7, 0), (0.7, 0), (0.7, 1)]
    cal = isotonic.fit(samples)
    ys = [b.y for b in cal.blocks]
    assert ys == sorted(ys)


def test_apply_returns_block_y_for_input_at_center():
    samples = [(0.2, 0), (0.2, 0), (0.8, 1), (0.8, 1)]
    cal = isotonic.fit(samples)
    assert isotonic.apply(cal, 0.2) == 0.0
    assert isotonic.apply(cal, 0.8) == 1.0


def test_apply_interpolates_between_blocks():
    samples = [(0.0, 0), (0.5, 0), (0.5, 1), (1.0, 1)]
    cal = isotonic.fit(samples)
    mid = isotonic.apply(cal, 0.25)
    assert 0.0 <= mid <= 1.0


def test_apply_clamps_at_extremes():
    samples = [(0.2, 0), (0.8, 1)]
    cal = isotonic.fit(samples)
    assert isotonic.apply(cal, -1.0) == isotonic.apply(cal, 0.2)
    assert isotonic.apply(cal, 2.0) == isotonic.apply(cal, 0.8)


def test_apply_none_calibrator_identity():
    assert isotonic.apply(None, 0.42) == 0.42


def test_brier_with_no_calibrator():
    samples = [(0.5, 1), (0.5, 0)]
    assert isotonic.brier(samples, None) == 0.25


def test_brier_improves_with_perfect_calibration():
    # Samples where raw p=0.1 always happens → calibrated should fix this.
    samples = [(0.1, 1)] * 20 + [(0.9, 0)] * 20
    cal = isotonic.fit(samples)
    raw = isotonic.brier(samples, None)
    cald = isotonic.brier(samples, cal)
    assert cald <= raw


def test_reliability_curve_monotone():
    samples = [(0.0, 0), (0.25, 0), (0.5, 1), (0.75, 1), (1.0, 1)] * 5
    cal = isotonic.fit(samples)
    curve = isotonic.reliability_curve(cal, 10)
    ys = [y for _, y in curve]
    assert all(ys[i] <= ys[i + 1] + 1e-9 for i in range(len(ys) - 1))


def _make_temp_db(tmp_path, rows):
    """Create a calibration.db-shaped sqlite at tmp_path with the rows
    inserted into prediction_snapshots. Each row is a dict of column→value."""
    import sqlite3
    db = tmp_path / "calibration.db"
    c = sqlite3.connect(db)
    c.execute("""
        CREATE TABLE prediction_snapshots (
            id INTEGER PRIMARY KEY,
            station_id TEXT NOT NULL,
            date TEXT NOT NULL,
            snapshot_time TEXT NOT NULL,
            slot INTEGER NOT NULL,
            is_auto INTEGER NOT NULL,
            expr TEXT NOT NULL,
            op TEXT NOT NULL,
            threshold REAL NOT NULL,
            bin_half REAL,
            predicted_p REAL NOT NULL,
            outcome INTEGER
        )
    """)
    for r in rows:
        c.execute("""INSERT INTO prediction_snapshots
                     (station_id, date, snapshot_time, slot, is_auto, expr,
                      op, threshold, predicted_p, outcome)
                     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                  (r["station_id"], r["date"], r["snapshot_time"],
                   r.get("slot", 1), r.get("is_auto", 0),
                   r.get("expr", ""), r["op"], r["threshold"],
                   r["predicted_p"], r.get("outcome")))
    c.commit()
    c.close()
    return db


def test_fit_from_db_dedupes_intra_day_polls(tmp_path, monkeypatch):
    # Same (station, date, op, threshold), three polls — only the latest
    # snapshot_time should be kept.
    rows = [
        dict(station_id="KX", date="2026-01-01", snapshot_time="08:00",
             op=">", threshold=70.0, predicted_p=0.20, outcome=1),
        dict(station_id="KX", date="2026-01-01", snapshot_time="12:00",
             op=">", threshold=70.0, predicted_p=0.40, outcome=1),
        dict(station_id="KX", date="2026-01-01", snapshot_time="16:00",
             op=">", threshold=70.0, predicted_p=0.90, outcome=1),
    ]
    db = _make_temp_db(tmp_path, rows)
    monkeypatch.setattr("calibration.DB_PATH", str(db))
    isotonic.invalidate_all()
    cal = isotonic.fit_from_db("KX")
    assert cal.n_fit == 1
    assert cal.blocks[0].x_min == 0.90  # latest snapshot kept


def test_fit_from_db_excludes_approx_equal_op(tmp_path, monkeypatch):
    # op='~' rows would inject p=1.0/outcome=0 noise; calibrator must skip them
    # and only fit on op IN ('>', '<').
    rows = [
        # op='~' garbage: p=1.0 but outcome=0 every time
        dict(station_id="KX", date="2026-01-01", snapshot_time="16:00",
             op="~", threshold=60.5, predicted_p=1.0, outcome=0),
        dict(station_id="KX", date="2026-01-02", snapshot_time="16:00",
             op="~", threshold=55.5, predicted_p=1.0, outcome=0),
        # op='>' real bets: monotone signal
        dict(station_id="KX", date="2026-01-03", snapshot_time="16:00",
             op=">", threshold=70.0, predicted_p=0.20, outcome=0),
        dict(station_id="KX", date="2026-01-04", snapshot_time="16:00",
             op=">", threshold=70.0, predicted_p=0.80, outcome=1),
    ]
    db = _make_temp_db(tmp_path, rows)
    monkeypatch.setattr("calibration.DB_PATH", str(db))
    isotonic.invalidate_all()
    cal = isotonic.fit_from_db("KX")
    assert cal.n_fit == 2  # the two op='>' only
    # And monotone (was antimonotone before, would have been pooled)
    ys = [b.y for b in cal.blocks]
    assert ys == sorted(ys)
