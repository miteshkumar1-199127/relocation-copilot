from pathlib import Path

import pytest

from relocation.config import Settings
from relocation.models import validate_evidence, validate_profile
from relocation.sources import SourceError, _public_https, discover_housing_sources, discover_stage_sources
from relocation.workflow import RelocationWorkflow, _candidate_ok


class FakeMemory:
    def recall(self, user_id, query):
        return ["Family prefers a 30-minute daycare commute"]


class FakeLLM:
    def __init__(self, fail_role=None):
        self.fail_role = fail_role

    def json(self, role, payload, allowed_ids):
        if role == self.fail_role:
            raise RuntimeError("simulated provider outage")
        result = {"summary": f"{role} checked", "actions": [], "questions": [],
                  "claims": [{"text": "Fixture observation", "source_ids": [next(iter(allowed_ids))]}],
                  "candidates": []}
        if role == "housing":
            result["candidates"] = [
                {"name": "A", "monthly_krw": 5000000, "daycare_commute_min": 20, "furnished": True, "source_ids": ["H1"]},
                {"name": "B", "monthly_krw": 3500000, "daycare_commute_min": 45, "furnished": False, "source_ids": ["H1"]},
                {"name": "C", "monthly_krw": 3900000, "daycare_commute_min": 25, "furnished": True, "source_ids": ["H1"]},
            ]
        return result


def profile():
    return {"origin": "Bangalore", "destination": "Seoul", "move_date": "2026-10-24",
            "family_size": 3, "housing_budget_krw": 4000000,
            "max_daycare_commute_min": 30, "citizenships": "Example",
            "visa_status": "Unknown", "workplace": "Example", "family_needs": "Daycare"}


def evidence():
    return [{"id": "H1", "domain": "housing", "title": "Fixture", "url": "https://example.org/housing",
             "retrieved_at": "2026-09-13T00:00:00Z", "text": "Fixture housing data for three sample apartments."}]


def test_profile_and_evidence_validation():
    assert validate_profile(profile())["move_date"] == "2026-10-24"
    with pytest.raises(ValueError):
        validate_profile({**profile(), "move_date": "October 24"})
    with pytest.raises(ValueError):
        validate_evidence([{**evidence()[0], "url": "http://example.org"}])


def test_candidate_constraints():
    assert _candidate_ok({"monthly_krw": 3900000, "daycare_commute_min": 25}, profile())[0]
    assert not _candidate_ok({"monthly_krw": 3500000, "daycare_commute_min": 45}, profile())[0]
    assert not _candidate_ok({"monthly_krw": None, "daycare_commute_min": 20}, profile())[0]


def test_source_blocks_private_addresses():
    with pytest.raises(SourceError):
        _public_https("http://example.org")
    with pytest.raises(SourceError):
        _public_https("https://127.0.0.1/")


def test_workflow_replans_and_requires_review(tmp_path: Path):
    workflow = RelocationWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM(), memory=FakeMemory())
    result = workflow.start("test-1", "owner", profile(), evidence())
    assert result["report"]["selection"]["name"] == "C"
    assert any("Monthly rent exceeds budget" in x for x in result["conflicts"])
    assert any("Daycare commute exceeds limit" in x for x in result["conflicts"])
    assert result.get("__interrupt__")
    assert not result.get("approval")
    resumed = workflow.resume("test-1", "approve", "Reviewed")
    assert resumed["approval"]["decision"] == "approve"
    assert workflow.get("test-1")["approval"]["note"] == "Reviewed"
    revised = workflow.start("test-1", "owner", {**profile(), "housing_budget_krw": 5500000,
                                                  "max_daycare_commute_min": 22}, evidence())
    assert revised["report"]["selection"]["name"] == "A"
    assert not revised.get("approval")
    assert revised.get("__interrupt__")


def test_housing_leads_remain_visible_when_budget_is_unknown(tmp_path: Path):
    workflow = RelocationWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM(), memory=FakeMemory())
    without_budget = {**profile(), "housing_budget_krw": None}
    report = workflow.start("test-leads", "owner", without_budget, evidence())["report"]
    assert report["selection"] is None
    assert [item["candidate"]["name"] for item in report["recommendations"]] == ["C", "A"]
    assert all(item["status"] == "needs_check" for item in report["recommendations"])
    assert all("Rent or housing budget is unknown" in item["caveats"] for item in report["recommendations"])


def test_housing_handoff_continues_to_finance_family_and_logistics(tmp_path: Path):
    workflow = RelocationWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM(), memory=FakeMemory())
    report = workflow.start("test-handoff", "owner", profile(), evidence())["report"]
    statuses = {task["id"]: task["status"] for task in report["tasks"]}
    assert statuses["housing"] == "done"
    assert all(statuses[role] == "needs_user" for role in ("finance", "family", "logistics"))
    assert "3,900,000" in report["findings"]["finance"]["summary"]
    assert "C" in report["findings"]["family"]["summary"]
    assert any(action["id"] == "travel" for action in report["next_actions"])
    assert any(action["id"] == "logistics" and "furnished" in action["detail"]
               for action in report["next_actions"])


def test_housing_discovery_fetches_attributable_pages(monkeypatch):
    calls = []
    monkeypatch.setattr("relocation.sources.search_you", lambda query, key: [
        {"url": "https://example.org/listing-a", "title": "A"},
        {"url": "https://example.org/listing-b", "title": "B"},
    ])
    def fake_fetch(url, domain, source_id):
        calls.append(url)
        return {"id": source_id, "domain": domain, "title": "Listing", "url": url,
                "retrieved_at": "2026-09-14T00:00:00Z", "text": "Monthly rent 3,900,000 KRW"}
    monkeypatch.setattr("relocation.sources.fetch_source", fake_fetch)
    found = discover_housing_sources(profile(), [], "test-key")
    assert len(found) == 2
    assert all(item["domain"] == "housing" and item["url"] in calls for item in found)
    assert discover_housing_sources(profile(), found, "test-key") == found


def test_stage_discovery_normalizes_scheme_less_you_urls(monkeypatch):
    monkeypatch.setattr("relocation.sources.search_you", lambda query, key: [{
        "url": "example.org/homes", "title": "Homes",
        "snippet": "A current housing result with enough descriptive text to support grounded planning."}])
    found = discover_stage_sources(profile(), "housing", [], "you-key")
    assert found[0]["url"] == "https://example.org/homes"


def test_seoul_housing_discovery_without_search_key(monkeypatch):
    def fake_fetch(url, domain, source_id):
        return {"id": source_id, "domain": domain, "title": "Seoul homes", "url": url,
                "retrieved_at": "2026-09-14T00:00:00Z", "text": "Current rental listings"}
    monkeypatch.setattr("relocation.sources.fetch_source", fake_fetch)
    found = discover_housing_sources(profile(), [], "")
    assert len(found) == 1
    assert found[0]["url"] == "https://seoulhomes.kr/en/properties/for-rent/"
    assert discover_housing_sources({**profile(), "destination": "Tokyo"}, [], "") == []


def test_specialist_failure_does_not_stop_other_work(tmp_path: Path):
    workflow = RelocationWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM("housing"), memory=FakeMemory())
    result = workflow.start("test-2", "owner", profile(), evidence())
    assert any("simulated provider outage" in x for x in result["errors"])
    assert result["report"]["selection"] is None
    assert next(t for t in result["tasks"] if t["id"] == "housing")["status"] == "failed"


def test_memory_startup_failure_keeps_plan_available(tmp_path: Path, monkeypatch):
    def broken_memory(settings):
        raise RuntimeError("memory store temporarily unavailable")

    monkeypatch.setattr("relocation.workflow.Mem0Store", broken_memory)
    workflow = RelocationWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM())
    result = workflow.start("test-memory", "owner", profile(), evidence())
    assert result["report"]["selection"]["name"] == "C"
    assert any("Memory retrieval failed" in error for error in result["errors"])
