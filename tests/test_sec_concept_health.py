import requests

from worldintel.core import sec_fundamentals as sf


def test_concept_http_failures_are_diagnosed_without_response_text(monkeypatch):
    response = requests.Response()
    response.status_code = 403
    error = requests.HTTPError("private request details", response=response)
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: (_ for _ in ()).throw(error))
    health = {}
    assert sf.fetch_concept("123", "Revenues", None, health=health) == {}
    assert health["sec_fundamentals"]["concept_requests"] == [{"cik": "123", "tag": "Revenues",
                                      "outcome": "http_error", "http_status": 403}]
    assert "private" not in str(health)


def test_missing_tag_is_distinct_from_failed_request(monkeypatch):
    response = requests.Response()
    response.status_code = 404
    error = requests.HTTPError(response=response)
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: (_ for _ in ()).throw(error))
    health = {}
    assert sf.fetch_concept("123", "Revenues", None, health=health) == {}
    assert health["sec_fundamentals"]["concept_requests"][0]["outcome"] == "tag_absent"


def test_failed_revenue_fetch_is_unavailable_rather_than_stale(monkeypatch):
    monkeypatch.setattr(sf, "fetch_submissions", lambda *_: {})
    monkeypatch.setattr(sf, "resolve_period", lambda *_: {"period_end": "2026-06-30"})
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: (_ for _ in ()).throw(requests.Timeout()))
    health = {}
    result = sf.build_fundamentals("123", "2026-07-30", None, health, sf._now() + 60)
    assert result["status"] == "unavailable"
    assert "revenue requests failed" in str(health["sec_fundamentals"])


def test_successful_empty_concepts_remain_stale(monkeypatch):
    monkeypatch.setattr(sf, "fetch_submissions", lambda *_: {})
    monkeypatch.setattr(sf, "resolve_period", lambda *_: {"period_end": "2026-06-30"})
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: {})
    health = {}
    result = sf.build_fundamentals("123", "2026-07-30", None, health, sf._now() + 60)
    assert result["status"] == "stale"
    assert all(entry["outcome"] == "ok" for entry in health["sec_fundamentals"]["concept_requests"])


def test_absent_revenue_tags_are_unavailable_not_a_stale_period(monkeypatch):
    monkeypatch.setattr(sf, "fetch_submissions", lambda *_: {})
    monkeypatch.setattr(sf, "resolve_period", lambda *_: {"period_end": "2026-06-30"})
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    response = requests.Response()
    response.status_code = 404
    error = requests.HTTPError(response=response)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: (_ for _ in ()).throw(error))
    health = {}
    result = sf.build_fundamentals("123", "2026-07-30", None, health, sf._now() + 60)
    assert result["status"] == "unavailable"
    assert "no supported revenue tags" in str(health["sec_fundamentals"])


def test_request_diagnostics_use_existing_source_health_entry(monkeypatch):
    monkeypatch.setattr(sf, "_sleep", lambda _: None)
    monkeypatch.setattr(sf, "_sec_get_json", lambda *_: {})
    health = {}
    sf.fetch_concept("123", "Revenues", None, health=health)
    assert set(health) == {"sec_fundamentals"}
    assert health["sec_fundamentals"]["ok"] is True
    sf._health_note(health, "unavailable")
    assert health["sec_fundamentals"]["ok"] is False
