import weather_alerts as wa


def _feat(**kwargs):
    props = {
        "id": kwargs.get("id", "urn:oid:test.123"),
        "event": kwargs.get("event", "Severe Thunderstorm Warning"),
        "severity": kwargs.get("severity", "Severe"),
        "urgency": kwargs.get("urgency", "Immediate"),
        "certainty": kwargs.get("certainty", "Observed"),
        "headline": kwargs.get("headline", "Test headline"),
        "effective": kwargs.get("effective", "2026-04-24T10:00:00-04:00"),
        "ends": kwargs.get("ends", "2026-04-24T14:00:00-04:00"),
        "expires": kwargs.get("expires", "2026-04-24T13:00:00-04:00"),
        "areaDesc": kwargs.get("areaDesc", "Suffolk, MA"),
        "senderName": kwargs.get("senderName", "NWS Boston MA"),
    }
    return {"type": "Feature", "properties": props}


def test_parse_features_skips_missing_id_or_event():
    feats = [
        {"properties": {}},
        {"properties": {"id": "x"}},
        {"properties": {"event": "Tornado Warning"}},
        _feat(),
    ]
    out = wa.parse_features(feats)
    assert len(out) == 1
    assert out[0].event == "Severe Thunderstorm Warning"


def test_parse_features_defaults_unknown_fields():
    feats = [{"type": "Feature", "properties": {
        "id": "urn:x", "event": "Frost Advisory",
    }}]
    out = wa.parse_features(feats)
    assert out[0].severity == "Unknown"
    assert out[0].urgency == "Unknown"
    assert out[0].certainty == "Unknown"
    assert out[0].area_desc == ""


def test_parse_features_prefers_ends_over_expires():
    f = _feat(ends="2026-04-24T14:00:00-04:00", expires="2026-04-24T13:00:00-04:00")
    out = wa.parse_features([f])
    assert out[0].ends == "2026-04-24T14:00:00-04:00"


def test_parse_features_falls_back_to_expires_when_no_ends():
    props = _feat()["properties"]
    props["ends"] = None
    out = wa.parse_features([{"properties": props}])
    assert out[0].ends == props["expires"]


def test_is_temp_relevant_drops_marine_and_flood():
    for irrelevant in (
        "Coastal Flood Advisory", "Rip Current Statement",
        "Small Craft Advisory", "River Flood Warning",
        "Beach Hazards Statement", "Air Quality Alert",
    ):
        assert not wa.is_temp_relevant(irrelevant)


def test_is_temp_relevant_keeps_temperature_events():
    for relevant in (
        "Severe Thunderstorm Warning", "Tornado Watch",
        "Winter Storm Warning", "Excessive Heat Warning",
        "Red Flag Warning", "High Wind Warning",
        "Wind Chill Advisory", "Frost Advisory",
        "Dense Fog Advisory",  # not marine, could affect obs
    ):
        assert wa.is_temp_relevant(relevant)


def test_short_id_is_deterministic_and_12_chars():
    a1 = wa.Alert(id="urn:oid:abc", event="E", severity="S", urgency="U",
                  certainty="C", headline="", effective=None, ends=None,
                  area_desc="", sender_name="")
    a2 = wa.Alert(id="urn:oid:abc", event="X", severity="", urgency="",
                  certainty="", headline="", effective=None, ends=None,
                  area_desc="", sender_name="")
    sid = a1.short_id()
    assert sid == a2.short_id()
    assert len(sid) == 12


def test_is_high_priority_severity():
    a = wa.Alert(id="x", event="Frost Advisory", severity="Severe",
                 urgency="Expected", certainty="Likely", headline="",
                 effective=None, ends=None, area_desc="", sender_name="")
    assert wa.is_high_priority(a)


def test_is_high_priority_urgency_immediate():
    a = wa.Alert(id="x", event="Frost Advisory", severity="Minor",
                 urgency="Immediate", certainty="Observed", headline="",
                 effective=None, ends=None, area_desc="", sender_name="")
    assert wa.is_high_priority(a)


def test_is_high_priority_event_keyword_match():
    a = wa.Alert(id="x", event="Tornado Watch", severity="Moderate",
                 urgency="Expected", certainty="Possible", headline="",
                 effective=None, ends=None, area_desc="", sender_name="")
    assert wa.is_high_priority(a)


def test_is_high_priority_ordinary_advisory_is_default():
    a = wa.Alert(id="x", event="Frost Advisory", severity="Minor",
                 urgency="Expected", certainty="Likely", headline="",
                 effective=None, ends=None, area_desc="", sender_name="")
    assert not wa.is_high_priority(a)
