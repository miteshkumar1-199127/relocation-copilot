"""Customer journeys through the complete Streamlit page and real workflow graph."""

from pathlib import Path

import streamlit as st
from streamlit.testing.v1 import AppTest

from relocation.config import Settings
from relocation.storage import RunIndex
from relocation.workflow import RelocationWorkflow as RealWorkflow


APP = Path(__file__).resolve().parents[1] / "app.py"


class FakeMemory:
    def __init__(self):
        self.facts = {}

    def recall(self, user_id, query):
        return list(self.facts.get(user_id, []))

    def remember(self, user_id, text):
        self.facts.setdefault(user_id, []).append(text)


class FakeLLM:
    def json(self, role, payload, allowed_ids):
        result = {"summary": f"{role} reviewed the available page", "actions": [],
                  "questions": [], "claims": [], "candidates": [],
                  "options": [], "steps": []}
        source_id = next(iter(allowed_ids))
        result["steps"] = [{"title": f"Complete {role} requirement",
                            "how_to": "Follow the instructions on the cited page.",
                            "done_when": "The source confirms completion", "source_ids": [source_id]}]
        if role in ("immigration", "logistics", "travel"):
            result["options"] = [{"name": f"Current {role} option",
                                  "description": "Details supported by the current source.",
                                  "source_ids": [source_id]}]
        if role == "housing":
            result["candidates"] = [
                {"name": "Riverside apartment", "monthly_krw": 3000000,
                 "daycare_commute_min": None, "furnished": True, "source_ids": [source_id]},
                {"name": "Park apartment", "monthly_krw": 3500000,
                 "daycare_commute_min": None, "furnished": False, "source_ids": [source_id]},
            ]
        return result


def profile(destination="Seoul"):
    return {"origin": "Bangalore", "destination": destination, "move_date": "2026-10-24",
            "family_size": 3, "housing_budget_krw": 4000000,
            "max_daycare_commute_min": None, "citizenships": "Indian",
            "visa_status": "To be checked", "workplace": destination,
            "family_needs": "Daycare"}


def source(domain="housing", url="https://example.org/homes"):
    return {"id": f"source-{domain}", "domain": domain, "title": "Current housing page",
            "url": url, "retrieved_at": "2026-09-14T00:00:00Z",
            "text": "Riverside apartment costs 3,000,000 KRW monthly. Park apartment costs 3,500,000 KRW monthly."}


def setup_journey(tmp_path, monkeypatch, *, discover_seoul=False, search_key=""):
    st.cache_resource.clear()
    original_settings = Settings
    monkeypatch.setattr("relocation.config.Settings",
                        lambda: original_settings(data_dir=tmp_path, you_key=search_key, persist_data=True))
    memory = FakeMemory()
    monkeypatch.setattr("relocation.workflow.RelocationWorkflow",
                        lambda settings: RealWorkflow(settings, llm=FakeLLM(), memory=memory))
    monkeypatch.setattr("relocation.sources.discover_housing_sources",
                        lambda profile, existing, key: [*existing, source()] if discover_seoul and
                        "seoul" in profile["destination"].lower() and not profile.get("skip_auto_housing_search") and
                        not any(item["domain"] == "housing" for item in existing) else existing)
    monkeypatch.setattr("relocation.sources.fetch_source",
                        lambda url, domain, source_id: {**source(domain, url), "id": source_id})
    return RunIndex(tmp_path / "runs.sqlite"), memory


def seed_move(index, tmp_path, thread_id, move_profile, evidence=None):
    flow = RealWorkflow(Settings(data_dir=tmp_path), llm=FakeLLM(), memory=FakeMemory())
    try:
        report = flow.start(thread_id, "owner", move_profile, evidence or [])["report"]
    finally:
        flow.connection.close()
    index.add(thread_id, "owner", move_profile, report)


def open_move(thread_id=None):
    app = AppTest.from_file(APP)
    if thread_id:
        app.session_state["thread_id"] = thread_id
    app.run(timeout=30)
    assert not app.exception
    return app


def labeled(widgets, label):
    return next(widget for widget in widgets if widget.label == label)


def test_new_move_starts_on_one_page_and_requires_route(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch, discover_seoul=True)
    app = open_move()
    assert not app.tabs
    assert any("What happens after you begin" in item.value for item in app.markdown)
    assert any("Visas &amp; immigration" in item.value and "Find a home" in item.value for item in app.markdown)
    labeled(app.button, "Create my relocation plan →").click().run(timeout=30)
    assert any("moving from and to" in message.value for message in app.error)
    labeled(app.text_input, "Moving from").set_value("Bangalore")
    labeled(app.text_input, "Moving to").set_value("Seoul")
    app.button(key="FormSubmitter:new_move-Create my relocation plan →").click().run(timeout=30)
    assert not app.exception
    saved = index.list("owner")
    assert len(saved) == 1
    report = index.get_snapshot(saved[0]["thread_id"])["report"]
    assert report["status"] == "needs_sources"
    assert report["evidence"] == []


def test_new_move_opens_when_external_workflow_is_unavailable(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)

    def unavailable_workflow(_settings):
        raise RuntimeError("provider temporarily unavailable")

    monkeypatch.setattr("relocation.workflow.RelocationWorkflow", unavailable_workflow)
    app = open_move()
    labeled(app.text_input, "Moving from").set_value("Bangalore")
    labeled(app.text_input, "Moving to").set_value("Tokyo")
    app.button[0].click().run(timeout=30)
    assert not app.exception
    saved = index.list("owner")
    assert len(saved) == 1
    report = index.get_snapshot(saved[0]["thread_id"])["report"]
    assert report["status"] == "needs_sources"
    assert report["profile"]["destination"] == "Tokyo"


def test_journey_resumes_at_housing_after_visa(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    move_profile = {**profile(), "completed_steps": ["immigration"], "skipped_steps": []}
    seed_move(index, tmp_path, "guided-journey", move_profile)
    app = open_move("guided-journey")
    assert labeled(app.number_input, "Monthly budget (KRW)")
    assert labeled(app.text_input, "School or daycare area")
    assert not any(widget.label == "Which path best matches your move?" for widget in app.radio)


def test_visa_choice_creates_checkpoint_flow(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    move_profile = {**profile("Tokyo"), "visa_status": ""}
    seed_move(index, tmp_path, "visa-journey", move_profile, [source("immigration", "https://example.org/visa")])
    app = open_move("visa-journey")
    labeled(app.radio, "Select the path you want to follow").set_value("Current immigration option")
    app.button(key="visa-journey_use_visa").click().run(timeout=30)
    assert not app.exception
    saved = index.get_snapshot("visa-journey")["report"]["profile"]
    assert saved["citizenships"] == "Indian"
    assert saved["visa_route"] == "Current immigration option"
    assert labeled(app.checkbox, "Mark this step complete")


def test_housing_preferences_produce_two_selectable_matches(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    move_profile = {**profile(), "completed_steps": ["immigration"]}
    seed_move(index, tmp_path, "home-journey", move_profile,
              [source(), source("finance", "https://example.org/finance")])
    app = open_move("home-journey")
    labeled(app.text_input, "School or daycare area").set_value("Gangnam")
    app.button(key="FormSubmitter:home-journey_home_preferences-Show my best matches").click().run(timeout=30)
    assert not app.exception
    assert sum(button.label == "Select this home" for button in app.button) == 2
    app.button(key="home-journey_select_home_0").click().run(timeout=30)
    assert index.get_snapshot("home-journey")["report"]["profile"]["preferred_home_name"]
    app.button(key="home-journey_finish_housing").click().run(timeout=30)
    assert labeled(app.checkbox, "Mark this step complete")
    assert app.button(key="home-journey_finish_finance")
    app.button(key="home-journey_finish_finance").click().run(timeout=30)
    assert labeled(app.multiselect, "What needs to be shipped?")


def test_completed_journey_shows_travel_documents(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    completed = ["immigration", "housing", "finance", "logistics", "travel"]
    seed_move(index, tmp_path, "ready-journey", {**profile(), "completed_steps": completed})
    app = open_move("ready-journey")
    assert any("You are ready to travel" in item.value for item in app.markdown)
    assert any("Passports and visas" in item.value for item in app.markdown)
    assert any("Your relocation plan in one view" in item.value for item in app.markdown)


def test_shipping_agency_choice_unblocks_travel_handoff(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    completed = ["immigration", "housing", "finance"]
    move_profile = {**profile(), "completed_steps": completed,
                    "shipping_items": ["Clothes", "Books"], "shipping_draft_ready": True}
    seed_move(index, tmp_path, "shipping-journey", move_profile,
              [source(), source("logistics", "https://example.org/shipping")])
    app = open_move("shipping-journey")
    app.button(key="shipping-journey_send_0").click().run(timeout=30)
    assert not app.exception
    saved = index.get_snapshot("shipping-journey")["report"]["profile"]
    assert saved["shipping_requests"] == ["Current logistics option"]
    assert app.button(key="shipping-journey_finish_logistics")
    app.button(key="shipping-journey_finish_logistics").click().run(timeout=30)
    assert any("Plan your final journey" in item.value for item in app.markdown)


def test_travel_agent_can_render_three_route_options(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    completed = ["immigration", "housing", "finance", "logistics"]
    seed_move(index, tmp_path, "travel-routes", {**profile(), "completed_steps": completed})
    report = index.get_snapshot("travel-routes")["report"]
    report.setdefault("findings", {})["travel"] = {
        "options": [{"name": f"Route {number}", "description": "Current route details",
                     "source_ids": ["travel-source"]} for number in range(1, 4)],
        "steps": [],
    }
    index.save_snapshot("travel-routes", report)
    app = open_move("travel-routes")
    assert not app.exception
    assert sum(button.label == "Choose this route" for button in app.button) == 3
    app.button(key="travel-routes_route_2").click().run(timeout=30)
    saved = index.get_snapshot("travel-routes")["report"]["profile"]
    assert saved["travel_route"]["name"] == "Route 3"
    assert "travel" in saved["completed_steps"]
    assert any("You are ready to travel" in item.value for item in app.markdown)


def test_current_relocation_is_primary_and_starting_over_is_secondary(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    seed_move(index, tmp_path, "current-move", profile(), [source()])
    app = open_move("current-move")
    assert not any(widget.label == "Your moves" for widget in app.selectbox)
    assert any("Bangalore to Seoul" in item.value for item in app.markdown)
    app.button(key="current-move_start_different").click().run(timeout=30)
    assert not app.exception
    assert labeled(app.text_input, "Moving from")


def test_reset_clears_saved_relocation_and_returns_home(tmp_path, monkeypatch):
    index, _ = setup_journey(tmp_path, monkeypatch)
    seed_move(index, tmp_path, "reset-me", profile())
    app = open_move("reset-me")
    app.button(key="reset_relocation").click().run(timeout=30)
    assert not app.exception
    assert index.list("owner") == []
    assert labeled(app.text_input, "Moving from")
