"""Movewell: one guided page for each relocation."""

from __future__ import annotations

import base64
import concurrent.futures
import html
import logging
import time
import uuid
from datetime import date
from logging.handlers import RotatingFileHandler
from pathlib import Path

import streamlit as st

from relocation.config import Settings
from relocation.models import DOMAINS
from relocation.providers import Nebius
from relocation.sources import (SourceError, discover_housing_sources, discover_stage_sources,
                                fetch_source, search_you)
from relocation.storage import EphemeralRunIndex, RunIndex
from relocation.workflow import RelocationWorkflow, _candidate_ok, next_actions


BASE_DIR = Path(__file__).resolve().parent
st.set_page_config(page_title="Movewell | Your move, made easier", page_icon="✈", layout="wide",
                   initial_sidebar_state="collapsed")
theme = (BASE_DIR / "assets/theme.css").read_text(encoding="utf-8")
hero_image = base64.b64encode((BASE_DIR / "assets/seoul-evening.webp").read_bytes()).decode("ascii")
st.markdown("<style>" + theme.replace("__HERO_IMAGE__", f"data:image/webp;base64,{hero_image}") + "</style>",
            unsafe_allow_html=True)
settings = Settings()
settings.prepare()
logger = logging.getLogger("movewell")
if not logger.handlers:
    handler = (RotatingFileHandler(settings.data_dir / "app.log", maxBytes=1_000_000, backupCount=2,
                                   encoding="utf-8") if settings.persist_data else logging.NullHandler())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.ERROR)

USER_ID = "owner"
LABELS = {
    "immigration": "Visas and documents", "housing": "Homes", "finance": "Money",
    "family": "Family", "logistics": "Moving your things", "travel": "Travel",
}
JOURNEY_STEPS = [
    ("immigration", "Visas & immigration", "Choose your visa route and prepare documents."),
    ("housing", "Find a home", "Balance home, school, budget and commute."),
    ("finance", "Set up your money", "Plan your budget, banking and insurance."),
    ("logistics", "Move your belongings", "Plan packing, shipping and storage."),
    ("travel", "Plan the journey", "Book travel and plan your arrival."),
]
@st.cache_resource
def run_index():
    return RunIndex(settings.data_dir / "runs.sqlite") if settings.persist_data else EphemeralRunIndex()


index = run_index()


def esc(value: object) -> str:
    return html.escape(str(value or ""), quote=True)


def move_currency(profile: dict) -> tuple[str, str]:
    if profile.get("currency_code"):
        code = str(profile["currency_code"])
    else:
        destination = str(profile.get("destination", "")).casefold()
        code = next((currency for names, currency in [
            (("seoul", "korea"), "KRW"), (("tokyo", "japan"), "JPY"),
            (("singapore",), "SGD"), (("london", "united kingdom", "uk"), "GBP"),
            (("dubai", "uae", "emirates"), "AED"), (("new york", "united states", "usa"), "USD"),
            (("sydney", "australia"), "AUD"), (("toronto", "canada"), "CAD"),
            (("paris", "france", "berlin", "germany"), "EUR")]
            if any(name in destination for name in names)), "USD")
    return code, {"KRW": "₩", "JPY": "¥", "GBP": "£", "EUR": "€", "USD": "$",
                  "SGD": "S$", "AED": "AED ", "AUD": "A$", "CAD": "C$"}.get(code, code + " ")


def section(title: str, subtitle: str = "") -> None:
    st.markdown(f'<div class="section-head"><div class="section">{esc(title)}</div>'
                f'<div class="sub">{esc(subtitle)}</div></div>',
                unsafe_allow_html=True)


@st.cache_resource(show_spinner="Getting your move ready...")
def workflow() -> RelocationWorkflow:
    return RelocationWorkflow(settings)


@st.cache_resource
def research_pool() -> concurrent.futures.ThreadPoolExecutor:
    return concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="movewell-research")


def load_move(thread_id: str) -> dict:
    state = index.get_snapshot(thread_id)
    if state is None:
        state = workflow().get(thread_id)
        if state.get("report"):
            index.save_snapshot(thread_id, state["report"], state.get("approval"))
    return state


def update_move(thread_id: str, profile: dict, sources: list[dict], *, discover: bool = False) -> dict:
    if discover:
        sources = discover_housing_sources(profile, sources, settings.you_key)
    result = workflow().start(thread_id, f"{USER_ID}:{thread_id}", profile, sources)
    index.save_snapshot(thread_id, result["report"], {})
    return result["report"]


def error_message(action: str, thread_id: str | None = None) -> None:
    reference = uuid.uuid4().hex[:8]
    logger.exception("%s failed (reference %s, move %s)", action, reference, thread_id or "new")
    st.error(f"We couldn't {action.lower()} just now. Please try again. If it happens again, share this code: {reference}.")


def plan_text(report: dict) -> str:
    profile = report["profile"]
    lines = ["MY MOVE", f"{profile['origin']} to {profile['destination']}",
             f"Moving on {profile['move_date']}", "", "NEXT STEPS"]
    for action in report.get("next_actions") or next_actions(report):
        lines.append(f"- {action['title']}: {action['detail']}")
    lines += ["", "HOUSING LEADS"]
    for item in report.get("recommendations", []):
        candidate = item["candidate"]
        lines.append(f"- {candidate.get('name', 'Home')}: {item['status']}")
    lines += ["", "SOURCES"]
    for source in report.get("evidence", []):
        lines.append(f"- {source['title']}: {source['url']}")
    lines += ["", "Confirm prices, availability and requirements with their original sources."]
    return "\n".join(lines)


def starter_report(profile: dict) -> dict:
    """Create a useful first plan without waiting for an external service."""
    questions = []
    if not profile.get("citizenships"):
        questions.append("What citizenships does your family hold?")
    if not profile.get("visa_status"):
        questions.append("What is your current visa situation?")
    if not profile.get("workplace"):
        questions.append("Where will you work or prefer to live?")
    if not profile.get("housing_budget_krw"):
        questions.append("What is your monthly home budget?")
    tasks = [{"id": "profile", "title": "Confirm family and move details",
              "depends_on": [], "status": "needs_user" if questions else "done",
              "reason": "Add the remaining details" if questions else ""}]
    for domain in DOMAINS:
        tasks.append({"id": domain, "title": f"Prepare {LABELS[domain].lower()}",
                      "depends_on": [], "status": "needs_user",
                      "reason": "Ready to work through"})
    report = {"profile": profile, "tasks": tasks, "findings": {}, "selection": None,
              "recommendations": [], "conflicts": [], "questions": questions,
              "issues": [], "evidence": [], "status": "needs_sources"}
    report["next_actions"] = next_actions(report)
    return report


def render_new_move() -> None:
    st.markdown(
        '<div class="move-planner-head"><div><span>RELOCATION PLANNER</span>'
        '<h2>Start your move</h2><p>Tell us where, when and who.</p></div>'
        '<div class="planner-promise"><b>Your whole move, connected</b><small>Five guided stages from visa to arrival</small></div></div>',
        unsafe_allow_html=True,
    )
    st.markdown('<div class="roadmap-intro"><b>What happens after you begin</b><span>Complete each stage in order, or skip anything already done.</span></div>',
                unsafe_allow_html=True)
    roadmap = "".join(
        f'<div class="roadmap-card"><i>{position:02d}</i><strong>{esc(title)}</strong><small>{esc(detail)}</small></div>'
        for position, (_, title, detail) in enumerate(JOURNEY_STEPS, 1))
    st.markdown(f'<div class="roadmap-grid">{roadmap}</div>', unsafe_allow_html=True)
    with st.form("new_move"):
        st.markdown('<div class="planner-step">1 &nbsp; YOUR RELOCATION</div>', unsafe_allow_html=True)
        cols = st.columns([3, .45, 3, 2.1, 1.6], vertical_alignment="bottom")
        origin = cols[0].text_input("Moving from", placeholder="City or country")
        cols[1].markdown('<div class="route-arrow">→</div>', unsafe_allow_html=True)
        destination = cols[2].text_input("Moving to", placeholder="City or country")
        move_date = cols[3].date_input("Moving on", value=date.today())
        family_size = cols[4].number_input("People moving", min_value=1, max_value=20, value=1)
        with st.expander("Personalize my plan (optional)"):
            st.markdown('<div class="planner-step">2 &nbsp; WHAT MATTERS TO YOU</div>', unsafe_allow_html=True)
            cols = st.columns(3)
            budget = cols[0].number_input("Monthly home budget (KRW)", min_value=0, step=100000,
                                          help="Leave this empty if you are still deciding.")
            workplace = cols[1].text_input("Workplace or preferred area", placeholder="Office or neighborhood")
            commute = cols[2].number_input("Longest daycare trip (minutes)", min_value=0, step=5)
            cols = st.columns(3)
            citizenships = cols[0].text_input("Citizenships", placeholder="For everyone moving")
            visa_status = cols[1].text_input("Visa situation", placeholder="What you know so far")
            family_needs = cols[2].text_input("Family needs", placeholder="Daycare, schools or healthcare")
            currency = st.selectbox("Home budget currency", ["USD", "KRW", "EUR", "GBP", "JPY", "SGD", "AED", "AUD", "CAD"])
            completed_steps = st.multiselect(
                "What have you already completed?",
                [step[0] for step in JOURNEY_STEPS],
                format_func=lambda key: next(title for step, title, _ in JOURNEY_STEPS if step == key),
                help="We will start with the first stage that still needs your attention.",
            )
        submitted = st.form_submit_button("Create my relocation plan →", type="primary", use_container_width=True)
    if submitted:
        if not origin.strip() or not destination.strip():
            st.error("Please add where you're moving from and to.")
            return
        profile = {"origin": origin.strip(), "destination": destination.strip(),
                   "move_date": move_date.isoformat(), "family_size": family_size,
                   "housing_budget_krw": budget or None, "max_daycare_commute_min": commute or None,
                   "citizenships": citizenships.strip(), "visa_status": visa_status.strip(),
                   "workplace": workplace.strip(), "family_needs": family_needs.strip(),
                   "currency_code": currency,
                   "completed_steps": completed_steps, "skipped_steps": []}
        thread_id = uuid.uuid4().hex
        try:
            # Save a complete local starting point first. External research enriches
            # it after the workspace opens and must never block move creation.
            index.add(thread_id, USER_ID, profile, starter_report(profile))
            st.session_state.thread_id = thread_id
            st.session_state.starting_new_relocation = False
            st.rerun()
        except Exception:
            error_message("Create your move")


def render_journey(thread_id: str, report: dict) -> bool:
    """Show progress and focus the customer on the first unfinished stage."""
    profile = report["profile"]
    completed = set(profile.get("completed_steps", []))
    skipped = set(profile.get("skipped_steps", []))
    current = next((step for step, _, _ in JOURNEY_STEPS if step not in completed | skipped), None)
    cards = []
    for position, (step, title, _) in enumerate(JOURNEY_STEPS, 1):
        status = "complete" if step in completed else "skipped" if step in skipped else "current" if step == current else "upcoming"
        label = "Done" if status == "complete" else "Skipped" if status == "skipped" else "Now" if status == "current" else "Next"
        cards.append(f'<div class="progress-stage {status}"><i>{position:02d}</i><span>{esc(title)}</span><small>{label}</small></div>')
    section("Your relocation journey", "One stage at a time. Complete it or skip ahead.")
    st.markdown(f'<div class="progress-roadmap">{"".join(cards)}</div>', unsafe_allow_html=True)
    if current is None:
        st.success("You have worked through every stage. Review the plan below and update it whenever something changes.")
        return True
    _, title, detail = next(item for item in JOURNEY_STEPS if item[0] == current)
    stage_help = {
        "immigration": "Add citizenship and visa details, then use an official immigration page to verify the correct route and document list.",
        "housing": "Set your budget and preferred area. Compare a sourced home, confirm its deposit and availability, then plan around it.",
        "finance": "Use the chosen home's rent and deposit to build the move budget. Add a trusted banking, tax or insurance page where needed.",
        "family": "Check care, school and healthcare options from the chosen neighborhood, including realistic travel times.",
        "logistics": "Confirm what the home includes. Use that to decide what to ship, store, sell or buy after arrival.",
        "travel": "Once documents are clear, compare travel dates, baggage needs and arrival transport for the whole household.",
    }
    st.markdown(f'<div class="current-stage"><span>STAGE {next(i for i, item in enumerate(JOURNEY_STEPS, 1) if item[0] == current):02d}</span>'
                f'<h3>{esc(title)}</h3><p>{esc(detail)}</p><div class="stage-help">{esc(stage_help[current])}</div></div>',
                unsafe_allow_html=True)
    cols = st.columns(2)
    if cols[0].button("I've completed this stage", type="primary", key=f"{thread_id}_complete_{current}",
                      use_container_width=True):
        try:
            revised = {**profile, "completed_steps": [*completed, current], "skipped_steps": list(skipped - {current})}
            update_move(thread_id, revised, report.get("evidence", []))
            st.rerun()
        except Exception:
            error_message("Update your journey", thread_id)
    if cols[1].button("Skip for now", key=f"{thread_id}_skip_{current}", use_container_width=True):
        try:
            revised = {**profile, "completed_steps": list(completed), "skipped_steps": [*skipped, current]}
            update_move(thread_id, revised, report.get("evidence", []))
            st.rerun()
        except Exception:
            error_message("Update your journey", thread_id)
    if skipped:
        with st.expander("Revisit skipped stages"):
            skipped_titles = [title for step, title, _ in JOURNEY_STEPS if step in skipped]
            st.write("Skipped: " + ", ".join(skipped_titles))
            if st.button("Bring skipped stages back", key=f"{thread_id}_restore_skipped"):
                try:
                    update_move(thread_id, {**profile, "skipped_steps": []}, report.get("evidence", []))
                    st.rerun()
                except Exception:
                    error_message("Restore skipped stages", thread_id)
    return False


def render_answers(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    prompts = [
        ("citizenships", "Passports", "What citizenships does your family hold?", "For everyone moving"),
        ("visa_status", "Visa situation", "What is your visa situation?", "What you know so far"),
        ("workplace", "Daily journey", "Where will you work or prefer to live?", "An office or area"),
        ("housing_budget_krw", "Home budget", "What is your monthly home budget (KRW)?", "Your limit"),
        ("family_needs", "Family life", "What does your family need nearby?", "Daycare or healthcare"),
    ]
    missing = [item for item in prompts if not profile.get(item[0]) and
               (item[0] != "family_needs" or profile.get("family_size", 1) > 1)]
    if not missing:
        return
    section("A few details will sharpen your plan", "Answer what you know now. You can change it later.")
    answers: dict = {}
    columns = st.columns(2)
    for position, (key, title, label, placeholder) in enumerate(missing):
        with columns[position % 2]:
            with st.container(border=True):
                st.markdown(f'<div class="answer-card-title">{esc(title)}</div>', unsafe_allow_html=True)
                widget_key = f"{thread_id}_answer_{key}"
                if key == "housing_budget_krw":
                    answers[key] = st.number_input(label, min_value=0, step=100000, key=widget_key)
                else:
                    answers[key] = st.text_input(label, placeholder=placeholder, key=widget_key)
    if st.button("Save these details", type="primary", key=f"{thread_id}_save_answers"):
        entered = {key: value for key, value in answers.items() if str(value).strip() and value != 0}
        if not entered:
            st.info("Add at least one detail to update your move.")
            return
        try:
            with st.spinner("Updating your move..."):
                update_move(thread_id, {**profile, **entered}, report.get("evidence", []))
            st.rerun()
        except Exception:
            error_message("Save these details", thread_id)


def render_homes(thread_id: str, report: dict) -> None:
    section("Homes to explore", "A lead is a starting point. Check availability, deposit and terms with its source.")
    recommendations = report.get("recommendations") or []
    if not recommendations:
        has_housing_page = any(source["domain"] == "housing" for source in report.get("evidence", []))
        guidance = ("The saved listings did not produce a suitable lead. Add another housing page below or change your limits."
                    if has_housing_page else "Add a housing page below or try the search again.")
        st.markdown('<div class="spotlight"><div class="tag">STILL SEARCHING</div>'
                    f'<h3>No suitable home lead yet</h3><p>{esc(guidance)}</p></div>',
                    unsafe_allow_html=True)
        if not has_housing_page and not report["profile"].get("skip_auto_housing_search") and (
            "seoul" in report["profile"]["destination"].lower() or settings.you_key):
            if st.button("Try home search again", key=f"{thread_id}_retry_homes"):
                try:
                    with st.spinner("Looking for homes..."):
                        updated = update_move(thread_id, report["profile"], report.get("evidence", []), discover=True)
                    if updated.get("recommendations"):
                        st.rerun()
                    else:
                        st.info("No suitable homes were confirmed. Try adding a housing page or changing your limits.")
                except Exception:
                    error_message("Search for homes", thread_id)
        return
    sources = {item["id"]: item for item in report.get("evidence", [])}
    for position, recommendation in enumerate(recommendations):
        candidate = recommendation["candidate"]
        name = str(candidate.get("name") or "Home lead")
        chosen = report["profile"].get("preferred_home_name") == name
        verified = recommendation["status"] == "verified"
        badges = []
        if candidate.get("monthly_krw") is not None:
            badges.append(f'KRW {int(candidate["monthly_krw"]):,} / month')
        if candidate.get("daycare_commute_min") is not None:
            badges.append(f'{int(candidate["daycare_commute_min"])} min to daycare')
        if candidate.get("furnished") is not None:
            badges.append("Furnished" if candidate["furnished"] else "Unfurnished")
        pill_html = "".join(f'<span class="pill">{esc(value)}</span>' for value in badges)
        caveats = "; ".join(recommendation.get("caveats", []))
        detail = "Fits the limits we could check." if verified else f"Needs checking: {caveats or 'some details are missing'}."
        st.markdown(f'<div class="spotlight"><div class="tag">{"YOUR PLANNING CHOICE" if chosen else "HOME LEAD"}</div>'
                    f'<h3>{esc(name)}</h3><p>{esc(detail)} Confirm availability and terms with the source.</p>'
                    f'{pill_html}</div>', unsafe_allow_html=True)
        for source_id in candidate.get("source_ids", []):
            if source_id in sources:
                st.link_button("View housing source", sources[source_id]["url"],
                               key=f"{thread_id}_home_source_{position}_{source_id}")
        if not chosen and st.button("Plan around this home", key=f"{thread_id}_choose_home_{position}"):
            try:
                with st.spinner("Updating the rest of your move..."):
                    update_move(thread_id, {**report["profile"], "preferred_home_name": name},
                                report.get("evidence", []))
                st.rerun()
            except Exception:
                error_message("Plan around this home", thread_id)


def render_actions(report: dict) -> None:
    section("Your next moves", "Each step builds on what is known so far.")
    for action in report.get("next_actions") or next_actions(report):
        st.markdown(f'<div class="next-action"><span class="next-action-state">{esc(action["status"].replace("_", " "))}</span>'
                    f'<strong>{esc(action["title"])}</strong><p>{esc(action["detail"])}</p></div>',
                    unsafe_allow_html=True)
    with st.expander("See progress across every part of your move"):
        for task in report.get("tasks", []):
            title = LABELS.get(task["id"], "Your move details")
            st.write(f"{title}: {task['status'].replace('_', ' ')}")
            finding = report.get("findings", {}).get(task["id"], {})
            if finding.get("summary") and finding["summary"] not in ("No source supplied", "Research failed"):
                st.caption(finding["summary"])


def render_sources(thread_id: str, report: dict) -> None:
    section("Helpful pages", "Add a page you trust. Your plan updates as soon as it is saved.")
    default_domain = "housing" if not any(item["domain"] == "housing" for item in report.get("evidence", [])) else "immigration"
    domain = st.selectbox("Page topic", DOMAINS, format_func=lambda item: LABELS[item],
                          index=DOMAINS.index(default_domain), key=f"{thread_id}_source_domain")
    with st.form(f"{thread_id}_add_source", clear_on_submit=True):
        url = st.text_input("Page link", placeholder="https://...")
        add = st.form_submit_button("Add page and update plan", type="primary")
    if add:
        if any(item["url"] == url.strip() for item in report.get("evidence", [])):
            st.info("That page is already in this move.")
        else:
            try:
                with st.spinner("Reading this page and updating your move..."):
                    source = fetch_source(url.strip(), domain, uuid.uuid4().hex[:12])
                    sources = [*report.get("evidence", []), source]
                    profile = dict(report["profile"])
                    if domain == "housing":
                        profile["skip_auto_housing_search"] = False
                    update_move(thread_id, profile, sources)
                st.rerun()
            except SourceError:
                st.error("We couldn't read that page. Try a public HTTPS page with readable text.")
            except Exception:
                error_message("Add this page", thread_id)
    if settings.you_key:
        with st.expander("Find a useful page"):
            query = st.text_input("Search for", key=f"{thread_id}_source_query",
                                  placeholder="Official visa information for South Korea")
            if st.button("Search pages", key=f"{thread_id}_source_search"):
                try:
                    st.session_state[f"{thread_id}_search_results"] = search_you(query, settings.you_key)
                except SourceError:
                    st.error("Search is unavailable right now. You can still paste a page link above.")
            for position, result in enumerate(st.session_state.get(f"{thread_id}_search_results", [])):
                st.write(result.get("title") or result.get("url"))
                st.caption(result.get("snippet", ""))
                if st.button("Use this page", key=f"{thread_id}_use_result_{position}"):
                    try:
                        if any(item["url"] == result["url"] for item in report.get("evidence", [])):
                            st.info("That page is already in this move.")
                            continue
                        with st.spinner("Adding this page..."):
                            source = fetch_source(result["url"], domain, uuid.uuid4().hex[:12])
                            update_move(thread_id, report["profile"], [*report.get("evidence", []), source])
                        st.rerun()
                    except SourceError:
                        st.error("We couldn't read that page. Try another result.")
                    except Exception:
                        error_message("Add this page", thread_id)
    if report.get("evidence"):
        with st.expander(f"Pages in this plan ({len(report['evidence'])})"):
            for source in report["evidence"]:
                columns = st.columns([5, 1], vertical_alignment="center")
                columns[0].link_button(f'{source["title"]} - {LABELS[source["domain"]]}', source["url"])
                if columns[1].button("Remove", key=f"{thread_id}_remove_{source['id']}"):
                    remaining = [item for item in report["evidence"] if item["id"] != source["id"]]
                    profile = dict(report["profile"])
                    if source["domain"] == "housing" and not any(item["domain"] == "housing" for item in remaining):
                        profile["skip_auto_housing_search"] = True
                    try:
                        with st.spinner("Updating your plan..."):
                            update_move(thread_id, profile, remaining)
                        st.rerun()
                    except Exception:
                        error_message("Remove this page", thread_id)


def render_details(thread_id: str, report: dict) -> None:
    with st.popover("Edit move details", use_container_width=True):
        profile = report["profile"]
        current_currency, _ = move_currency(profile)
        with st.form(f"{thread_id}_edit_move"):
            columns = st.columns(3)
            origin = columns[0].text_input("Moving from", value=profile["origin"])
            destination = columns[1].text_input("Moving to", value=profile["destination"])
            move_date = columns[2].date_input("Move date", value=date.fromisoformat(profile["move_date"]))
            columns = st.columns(4)
            family_size = columns[0].number_input("People moving", min_value=1, max_value=20,
                                                  value=int(profile.get("family_size", 1)))
            currency_options = ["USD", "KRW", "EUR", "GBP", "JPY", "SGD", "AED", "AUD", "CAD"]
            currency = columns[1].selectbox("Home budget currency", currency_options,
                                            index=currency_options.index(current_currency))
            budget_step = 100000 if currency in {"KRW", "JPY"} else 100
            budget = columns[2].number_input(f"Monthly home budget ({currency})", min_value=0,
                                             value=int(profile.get("housing_budget_krw") or 0), step=budget_step)
            commute = columns[3].number_input("Longest school trip (minutes)", min_value=0,
                                              value=int(profile.get("max_daycare_commute_min") or 0), step=5)
            citizenships = st.text_input("Citizenships", value=profile.get("citizenships", ""))
            visa_status = st.text_input("Visa situation", value=profile.get("visa_status", ""))
            workplace = st.text_input("Workplace or preferred area", value=profile.get("workplace", ""))
            family_needs = st.text_input("Family needs", value=profile.get("family_needs", ""))
            save = st.form_submit_button("Save changes", type="primary")
        if save:
            if not origin.strip() or not destination.strip():
                st.error("Add both your starting point and destination.")
                return
            new_destination = destination.strip()
            changed_destination = new_destination.casefold() != profile["destination"].casefold()
            revised = {**profile, "origin": origin.strip(), "destination": new_destination,
                       "move_date": move_date.isoformat(), "family_size": family_size,
                       "housing_budget_krw": budget or None, "max_daycare_commute_min": commute or None,
                       "currency_code": currency,
                       "citizenships": citizenships.strip(), "visa_status": visa_status.strip(),
                       "workplace": workplace.strip(), "family_needs": family_needs.strip()}
            sources = report.get("evidence", [])
            if changed_destination:
                sources = []
                revised.pop("preferred_home_name", None)
                revised["skip_auto_housing_search"] = False
            try:
                with st.spinner("Updating your move..."):
                    update_move(thread_id, revised, sources)
                st.rerun()
            except Exception:
                error_message("Save your move changes", thread_id)


def render_preferences(thread_id: str, report: dict) -> None:
    with st.expander("What should we remember about your move?"):
        preference = st.text_area("Your preference", key=f"{thread_id}_preference",
                                  placeholder="We prefer daycare within 30 minutes of home.")
        if st.button("Remember and update my plan", key=f"{thread_id}_remember"):
            if not preference.strip():
                st.info("Add a preference first.")
            else:
                try:
                    with st.spinner("Saving your preference..."):
                        workflow().memory.remember(f"{USER_ID}:{thread_id}", preference.strip())
                        update_move(thread_id, report["profile"], report.get("evidence", []))
                    st.rerun()
                except Exception:
                    error_message("Save your preference", thread_id)


def render_review(thread_id: str, state: dict, report: dict) -> None:
    with st.expander("Review and keep a copy"):
        if state.get("approval"):
            st.success("You reviewed this version of your move plan.")
        else:
            st.write("Review what is known so far. This does not book or pay for anything.")
            note = st.text_input("Your note (optional)", key=f"{thread_id}_review_note")
            if st.button("I've reviewed this plan", key=f"{thread_id}_review"):
                try:
                    reviewed = workflow().resume(thread_id, "approve", note)
                    index.save_snapshot(thread_id, report, reviewed.get("approval"))
                    st.rerun()
                except Exception:
                    error_message("Save your review", thread_id)
        st.download_button("Download my move plan", plan_text(report),
                           f"my-move-{thread_id[:8]}.txt", "text/plain")


def save_journey(thread_id: str, report: dict, changes: dict | None = None,
                 *, complete: str | None = None) -> None:
    """Persist a journey decision without blocking on external research."""
    revised = dict(report)
    profile = {**report["profile"], **(changes or {})}
    completed = list(dict.fromkeys(profile.get("completed_steps", [])))
    if complete and complete not in completed:
        completed.append(complete)
    profile["completed_steps"] = completed
    profile["skipped_steps"] = [step for step in profile.get("skipped_steps", []) if step != complete]
    revised["profile"] = profile
    revised["next_actions"] = next_actions(revised)
    index.save_snapshot(thread_id, revised, {})


def journey_header(profile: dict) -> str | None:
    completed = set(profile.get("completed_steps", []))
    current = next((step for step, _, _ in JOURNEY_STEPS if step not in completed), None)
    cards = []
    for position, (step, title, _) in enumerate(JOURNEY_STEPS, 1):
        status = "complete" if step in completed else "current" if step == current else "upcoming"
        label = "Done" if status == "complete" else "You are here" if status == "current" else "Next"
        cards.append(f'<div class="progress-stage {status}"><i>{position:02d}</i>'
                     f'<span>{esc(title)}</span><small>{label}</small></div>')
    st.markdown(f'<div class="progress-roadmap">{"".join(cards)}</div>', unsafe_allow_html=True)
    return current


def complete_button(thread_id: str, report: dict, stage: str, label: str) -> None:
    if st.button(label, type="primary", use_container_width=True, key=f"{thread_id}_finish_{stage}"):
        save_journey(thread_id, report, complete=stage)
        st.rerun()


def run_stage_research(thread_id: str, report: dict, stage: str) -> dict:
    """Run external research away from Streamlit's render thread."""
    sources = discover_stage_sources(report["profile"], stage, report.get("evidence", []), settings.you_key)
    stage_sources = [item for item in sources if item["domain"] == stage]
    if not stage_sources:
        raise SourceError("No current source was returned by web research")
    profile = report["profile"]
    relevant_profile = {key: profile.get(key) for key in (
        "origin", "destination", "move_date", "family_size", "citizenships", "visa_status",
        "housing_budget_krw", "max_daycare_commute_min", "workplace", "school_area",
        "family_needs", "home_preferences", "preferred_home", "currency_code", "employment_path",
        "dependent_visas", "finance_needs", "monthly_transfer", "travel_cabin", "travel_stops",
        "travel_bags", "travel_priorities") if profile.get(key) not in (None, "", [])}
    instructions = ("Extract exactly two concrete current housing listings when the sources support them. Add no more than four concise customer steps. Keep claims, actions, questions and options minimal. Use null for any rent, commute, or furnishing detail absent from the sources." if stage == "housing" else
                    "Give concise customer options and detailed steps with a clear completion condition.")
    response_budget = {"housing": 1800, "finance": 1100, "immigration": 1000, "logistics": 1000, "travel": 1000, "family": 1000}.get(stage, 1200)
    payload = {"profile": relevant_profile, "evidence": stage_sources, "instructions": instructions}
    allowed_ids = {item["id"] for item in stage_sources}
    try:
        finding = Nebius(settings, settings.research_model).json(
            stage, payload, allowed_ids, max_tokens=response_budget)
    except Exception:
        if settings.research_model == settings.nebius_model:
            raise
        finding = Nebius(settings, settings.nebius_model).json(
            stage, payload, allowed_ids, max_tokens=response_budget)
    if stage == "housing" and not finding.get("candidates"):
        raise RuntimeError("The housing specialist found sources but could not extract any listings")
    if stage != "housing" and not (finding.get("options") or finding.get("steps")):
        raise RuntimeError(f"The {stage} specialist returned no usable guidance")
    researched = dict(report)
    researched["evidence"] = sources
    researched["findings"] = {**report.get("findings", {}), stage: finding}
    tasks = [dict(item) for item in report.get("tasks", [])]
    for task in tasks:
        if task.get("id") == stage:
            task.update(status="done", reason="Current sources reviewed")
    researched["tasks"] = tasks
    if stage == "housing":
        recommendations = []
        closest = []
        for candidate in finding.get("candidates", []):
            okay, reasons = _candidate_ok(candidate, profile)
            hard = {"Monthly rent exceeds budget", "Daycare commute exceeds limit",
                    "Rent is not a valid number", "Daycare commute is not a valid number"}
            item = {"candidate": candidate,
                    "status": "verified" if okay else "needs_check",
                    "caveats": reasons}
            if any(reason in hard for reason in reasons):
                item["status"] = "outside_preferences"
                closest.append(item)
            else:
                recommendations.append(item)
        if not recommendations:
            # A valid search with imperfect matches is useful customer output, not a system failure.
            recommendations = closest
        researched["recommendations"] = recommendations[:2]
    researched["next_actions"] = next_actions(researched)
    return researched


@st.fragment(run_every=2)
def research_stage(thread_id: str, report: dict, stage: str, *, auto_start: bool = True) -> None:
    label = LABELS[stage]
    job_key = f"{thread_id}_research_job_{stage}"
    started_key = f"{job_key}_started"
    failure_key = f"{job_key}_failure"
    if st.session_state.get(failure_key):
        st.error(st.session_state[failure_key])
        if st.button("Try research again", key=f"{thread_id}_retry_{stage}"):
            st.session_state.pop(failure_key, None)
            st.session_state[f"{thread_id}_research_requested_{stage}"] = True
            st.rerun()
        return
    future = st.session_state.get(job_key)
    if future is not None:
        if future.done():
            try:
                researched = future.result()
                index.save_snapshot(thread_id, researched, {})
                del st.session_state[job_key]
                st.session_state.pop(started_key, None)
                st.rerun()
            except Exception:
                del st.session_state[job_key]
                st.session_state.pop(started_key, None)
                reference = uuid.uuid4().hex[:8]
                logger.exception("Research %s failed (reference %s, move %s)", label.lower(), reference, thread_id)
                st.session_state[failure_key] = (
                    f"Research could not finish. Try again. If it keeps happening, share code {reference}.")
                st.rerun()
            return
        elapsed = int(time.monotonic() - st.session_state.get(started_key, time.monotonic()))
        st.markdown(f'<div class="research-panel"><div class="research-orbit"><i></i><i></i><i></i></div>'
                    f'<div class="research-copy"><b>{esc(label)} specialist is building your options</b>'
                    f'<span>Searching trusted pages, comparing choices and preparing your guide</span>'
                    f'<div class="research-track"><i></i></div><small>Working in the background · {elapsed}s</small>'
                    f'</div></div>',
                    unsafe_allow_html=True)
        return
    if auto_start:
        if not settings.you_key:
            st.error("Web research is not connected. Add YOU_API_KEY and restart the app.")
            return
        try:
            st.session_state[job_key] = research_pool().submit(run_stage_research, thread_id, report, stage)
            st.session_state[started_key] = time.monotonic()
            st.rerun()
        except Exception:
            error_message(f"Research {label.lower()}", thread_id)


def agent_finding(report: dict, stage: str) -> dict:
    return report.get("findings", {}).get(stage, {})


def consume_research_request(thread_id: str, stage: str) -> bool:
    """Consume a one-run user intent signal; saved preferences never launch agents by themselves."""
    return bool(st.session_state.pop(f"{thread_id}_research_requested_{stage}", False))


def render_item_links(report: dict, source_ids: list[str]) -> None:
    sources = {item["id"]: item for item in report.get("evidence", [])}
    links = [source for source_id in source_ids if (source := sources.get(source_id))]
    if links:
        safe_links = []
        for source in links:
            title = str(source.get("title", "Official details")).replace("[", "").replace("]", "")
            safe_links.append(f"[Read: {title}]({source['url']})")
        st.markdown(" · ".join(safe_links))


def render_agent_steps(thread_id: str, report: dict, stage: str) -> list[str]:
    finding = agent_finding(report, stage)
    steps = finding.get("steps", [])
    if not steps:
        return []
    st.markdown('<div class="decision-title">What to do</div>', unsafe_allow_html=True)
    completed = set(report["profile"].get(f"{stage}_guide_steps", []))
    selected = []
    for position, step in enumerate(steps, 1):
        title = str(step.get("title", f"Step {position}"))
        st.markdown(f'<div class="guide-step"><i>{position}</i><div><b>{esc(title)}</b>'
                    f'<p>{esc(step.get("how_to", ""))}</p><small>Done when: '
                    f'{esc(step.get("done_when", "You have confirmed this step"))}</small></div></div>',
                    unsafe_allow_html=True)
        render_item_links(report, step.get("source_ids", []))
        if st.checkbox("Mark this step complete", value=title in completed,
                       key=f"{thread_id}_{stage}_guide_{position}"):
            selected.append(title)
    if st.button("Save my progress", key=f"{thread_id}_save_{stage}_guide"):
        save_journey(thread_id, report, {f"{stage}_guide_steps": selected})
        st.rerun()
    return selected


def render_stage_sources(report: dict, stage: str) -> None:
    sources = [item for item in report.get("evidence", []) if item["domain"] == stage]
    if sources:
        with st.expander(f"Sources checked ({len(sources)})"):
            for source in sources:
                st.link_button(source["title"], source["url"])


def visa_stage(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    section("Choose your visa path", "Start here before committing to a home or travel.")
    st.markdown('<div class="stage-note">Add your citizenship, then let the immigration specialist check current '
                'sources for the visa paths that fit this move.</div>',
                unsafe_allow_html=True)
    with st.form(f"{thread_id}_visa_profile"):
        citizenships = st.text_input("Citizenship", value=profile.get("citizenships", ""),
                                     placeholder="For example, Indian")
        employment_path = st.selectbox("What brings you to work there?",
                                       ["A new local employer", "Transfer within my company",
                                        "I will look for work", "Self-employed or starting a business",
                                        "I already have work permission"],
                                       index=0)
        moving_with_family = st.checkbox("My partner or children need dependent visas",
                                         value=bool(profile.get("family_size", 1) > 1))
        save = st.form_submit_button("Find my visa options", type="primary")
    if save:
        if not citizenships.strip():
            st.error("Add your citizenship so the visa specialist can research the right routes.")
            return
        save_journey(thread_id, report, {"citizenships": citizenships.strip(),
                     "employment_path": employment_path, "dependent_visas": moving_with_family,
                     "visa_preferences_saved": True})
        st.session_state[f"{thread_id}_research_requested_immigration"] = True
        st.rerun()
    finding = agent_finding(report, "immigration")
    options = finding.get("options", [])
    if not options:
        research_stage(thread_id, report, "immigration",
                       auto_start=consume_research_request(thread_id, "immigration"))
        return
    st.markdown('<div class="decision-title">Available work visa paths</div>', unsafe_allow_html=True)
    names = [str(item.get("name", "Visa path")) for item in options]
    route = st.radio("Select the path you want to follow", names,
                     index=names.index(profile["visa_route"]) if profile.get("visa_route") in names else 0)
    selected_option = options[names.index(route)]
    st.markdown(f'<div class="option-detail"><b>{esc(route)}</b><p>{esc(selected_option.get("description", ""))}</p></div>',
                unsafe_allow_html=True)
    render_item_links(report, selected_option.get("source_ids", []))
    if profile.get("visa_route") == route:
        st.success(f"Selected visa path: {route}")
    if st.button("Choose this visa path", type="primary", key=f"{thread_id}_use_visa"):
        save_journey(thread_id, report, {"visa_route": route, "visa_status": route})
        st.rerun()
    done = render_agent_steps(thread_id, report, "immigration")
    render_stage_sources(report, "immigration")
    if profile.get("visa_route"):
        remaining = max(0, len(finding.get("steps", [])) - len(done))
        if remaining:
            st.caption(f"{remaining} visa checkpoint{'s' if remaining != 1 else ''} still open. Your progress is saved.")
        complete_button(thread_id, report, "immigration", "Continue with this visa path")


def housing_stage(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    currency_code, currency_symbol = move_currency(profile)
    section("Find the right home", "Tell us what daily life should look like, then choose one home.")
    with st.form(f"{thread_id}_home_preferences"):
        cols = st.columns(2)
        default_budget = int(profile.get("housing_budget_krw") or (3000000 if currency_code in ("KRW", "JPY") else 3000))
        step_size = 100000 if currency_code == "KRW" else 10000 if currency_code == "JPY" else 100
        budget = cols[0].number_input(f"Monthly budget ({currency_code})", min_value=step_size,
                                      value=default_budget, step=step_size)
        workplace = cols[1].text_input("Office or preferred area", value=profile.get("workplace", ""))
        cols = st.columns(2)
        school = cols[0].text_input("School or daycare area", value=profile.get("school_area", ""))
        commute = cols[1].slider("Maximum commute", 10, 90,
                                 int(profile.get("max_daycare_commute_min") or 30), 5, format="%d min")
        preferences = st.multiselect("What matters at home?",
                                     ["Furnished", "Near a park", "Near public transport", "Pet friendly", "Extra bedroom"],
                                     default=profile.get("home_preferences", []))
        find = st.form_submit_button("Show my best matches", type="primary")
    if find:
        save_journey(thread_id, report, {"housing_budget_krw": budget, "workplace": workplace.strip(),
                     "school_area": school.strip(), "max_daycare_commute_min": commute,
                     "home_preferences": preferences, "housing_preferences_saved": True,
                     "currency_code": currency_code})
        st.session_state[f"{thread_id}_research_requested_housing"] = True
        st.rerun()
    recommendations = report.get("recommendations", [])[:2]
    if not recommendations:
        st.markdown('<div class="stage-note">Review the preferences above, then choose <b>Show my best matches</b> '
                    'when you are ready for the housing specialist to search.</div>', unsafe_allow_html=True)
        research_stage(thread_id, report, "housing",
                       auto_start=consume_research_request(thread_id, "housing"))
        return
    exact_matches = sum(item.get("status") == "verified" for item in recommendations)
    match_label = (f"{exact_matches} match{'es' if exact_matches != 1 else ''} within your preferences"
                   if exact_matches else f"{len(recommendations)} closest sourced options")
    st.markdown(f'<div class="decision-title">{match_label}</div>', unsafe_allow_html=True)
    if not exact_matches:
        st.info("Current listings fall outside at least one preference. Compare the closest options below or adjust your move details.")
    cols = st.columns(2)
    sources = {item["id"]: item for item in report.get("evidence", [])}
    for pos, recommendation in enumerate(recommendations):
        home = recommendation["candidate"]
        with cols[pos]:
            name = str(home.get("name", "Home listing"))
            chosen = profile.get("preferred_home_name") == name
            rent = f'{currency_symbol}{int(home["monthly_krw"]):,} {currency_code} / month' if home.get("monthly_krw") else "Ask for rent"
            commute = f'{home["daycare_commute_min"]} min family commute' if home.get("daycare_commute_min") else "Commute needs checking"
            furnished = "Furnished" if home.get("furnished") is True else "Unfurnished" if home.get("furnished") is False else "Furnishing needs checking"
            caveats = recommendation.get("caveats", [])
            st.markdown(f'<div class="choice-card {"selected" if chosen else ""}"><span>{esc(recommendation["status"].replace("_", " ").title())}</span>'
                        f'<h3>{esc(name)}</h3><b>{rent}</b><p>{commute} · {furnished}</p></div>', unsafe_allow_html=True)
            if caveats:
                st.caption("Check: " + "; ".join(caveats))
            source_id = next(iter(home.get("source_ids", [])), None)
            if source_id in sources:
                st.link_button("Open listing", sources[source_id]["url"], use_container_width=True)
            if st.button("Select this home", key=f"{thread_id}_select_home_{pos}", use_container_width=True):
                save_journey(thread_id, report, {"preferred_home_name": name, "preferred_home": home})
                st.rerun()
            if st.button("Contact owner", key=f"{thread_id}_contact_home_{pos}", use_container_width=True):
                st.success("Your enquiry is ready. Owner contact is a preview in this version.")
    if profile.get("preferred_home_name"):
        render_agent_steps(thread_id, report, "housing")
        render_stage_sources(report, "housing")
        complete_button(thread_id, report, "housing", "Home finalized — continue to finances")


def finance_stage(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    currency_code, currency_symbol = move_currency(profile)
    section("Set up your finances", "Use your chosen home to prepare the money side of the move.")
    home = profile.get("preferred_home", {})
    rent = int(home.get("monthly_krw") or home.get("rent") or profile.get("housing_budget_krw") or 0)
    st.markdown(f'<div class="money-summary"><span>Plan around</span><strong>{currency_symbol}{rent:,} {currency_code} monthly rent</strong>'
                '<p>Keep funds ready for the deposit, first month, insurance and setup costs.</p></div>', unsafe_allow_html=True)
    finding = agent_finding(report, "finance")
    if not profile.get("finance_preferences_saved") and not finding.get("steps"):
        with st.form(f"{thread_id}_finance_preferences"):
            banking = st.multiselect("What financial help do you need?",
                                     ["Open a local bank account", "Transfer money internationally",
                                      "Pay rent and deposit", "Health insurance", "Home insurance",
                                      "Understand local taxes"])
            monthly_transfer = st.number_input(f"Expected monthly transfer ({currency_code})", min_value=0,
                                               step=100)
            save_finance = st.form_submit_button("Build my finance plan", type="primary")
        if save_finance:
            if not banking:
                st.error("Choose at least one area for the finance specialist.")
                return
            save_journey(thread_id, report, {"finance_needs": banking,
                         "monthly_transfer": monthly_transfer or None, "finance_preferences_saved": True})
            st.session_state[f"{thread_id}_research_requested_finance"] = True
            st.rerun()
        return
    if not finding.get("steps"):
        research_stage(thread_id, report, "finance",
                       auto_start=consume_research_request(thread_id, "finance"))
        return
    checked = render_agent_steps(thread_id, report, "finance")
    remaining = max(0, len(finding["steps"]) - len(checked))
    if remaining:
        st.caption(f"{remaining} checklist item{'s' if remaining != 1 else ''} still open. You can return to them later.")
    complete_button(thread_id, report, "finance", "Mark finances complete — continue to shipping")
    render_stage_sources(report, "finance")


def logistics_stage(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    section("Move your belongings", "Choose what is going, then request quotes from suitable movers.")
    categories = st.multiselect("What needs to be shipped?",
                                ["Clothes", "Kitchen items", "Books", "Toys", "Electronics", "Furniture", "Artwork"],
                                default=profile.get("shipping_items", []), key=f"{thread_id}_shipping_items")
    notes = st.text_area("Anything the mover should know?", value=profile.get("shipping_notes", ""),
                         placeholder="Approximate boxes, fragile items, pickup access…")
    if st.button("Find movers and create my request", type="primary", key=f"{thread_id}_create_quote"):
        save_journey(thread_id, report, {"shipping_items": categories, "shipping_notes": notes,
                                         "shipping_draft_ready": bool(categories)})
        if categories:
            st.session_state[f"{thread_id}_research_requested_logistics"] = True
        st.rerun()
    if not profile.get("shipping_draft_ready"):
        return
    items_text = ", ".join(profile.get("shipping_items", []))
    st.markdown(f'<div class="email-preview"><span>QUOTE REQUEST</span><b>Household move to {esc(profile["destination"])}</b>'
                f'<p>Please quote for shipping: {esc(items_text)}. Pickup is in {esc(profile["origin"])}. '
                f'Target move date: {esc(profile["move_date"])}.</p></div>', unsafe_allow_html=True)
    finding = agent_finding(report, "logistics")
    agencies = finding.get("options", [])
    if not agencies:
        research_stage(thread_id, report, "logistics",
                       auto_start=consume_research_request(thread_id, "logistics"))
        return
    if profile.get("shipping_requests"):
        st.success("Quote request prepared for " + ", ".join(profile["shipping_requests"]) + ".")
    st.markdown('<div class="decision-title">Shipping agencies found</div>', unsafe_allow_html=True)
    for pos, option in enumerate(agencies):
        agency = str(option.get("name", "Shipping agency"))
        cols = st.columns([4, 1.3], vertical_alignment="center")
        cols[0].markdown(f'<div class="agency"><div><b>{esc(agency)}</b><p>{esc(option.get("description", ""))}</p></div></div>', unsafe_allow_html=True)
        with cols[0]:
            render_item_links(report, option.get("source_ids", []))
        if cols[1].button("Send request", key=f"{thread_id}_send_{pos}", use_container_width=True):
            sent = list(dict.fromkeys([*profile.get("shipping_requests", []), agency]))
            save_journey(thread_id, report, {"shipping_requests": sent})
            st.rerun()
    if profile.get("shipping_requests"):
        render_agent_steps(thread_id, report, "logistics")
        render_stage_sources(report, "logistics")
        complete_button(thread_id, report, "logistics", "Shipping arranged — continue to travel")


def travel_stage(thread_id: str, report: dict) -> None:
    profile = report["profile"]
    section("Plan your final journey", f"Travel options for {profile['move_date']}.")
    finding = agent_finding(report, "travel")
    if not profile.get("travel_preferences_saved") and not finding.get("options"):
        with st.form(f"{thread_id}_travel_preferences"):
            cols = st.columns(2)
            cabin = cols[0].selectbox("Cabin", ["Economy", "Premium economy", "Business"])
            stops = cols[1].selectbox("Stops", ["Direct flights first", "Up to one stop", "Any route"])
            baggage = st.number_input("Checked bags for the household", min_value=0, max_value=20,
                                      value=max(1, int(profile.get("family_size", 1))))
            priorities = st.multiselect("What matters most?",
                                        ["Lowest fare", "Shortest journey", "Best baggage allowance",
                                         "Convenient arrival time", "Fewest connections"])
            find_routes = st.form_submit_button("Find my travel options", type="primary")
        if find_routes:
            save_journey(thread_id, report, {"travel_cabin": cabin, "travel_stops": stops,
                         "travel_bags": baggage, "travel_priorities": priorities,
                         "travel_preferences_saved": True})
            st.session_state[f"{thread_id}_research_requested_travel"] = True
            st.rerun()
        return
    routes = finding.get("options", [])[:3]
    if not routes:
        research_stage(thread_id, report, "travel",
                       auto_start=consume_research_request(thread_id, "travel"))
        return
    for row_start in range(0, len(routes), 2):
        cols = st.columns(2)
        for offset, route in enumerate(routes[row_start:row_start + 2]):
            pos = row_start + offset
            with cols[offset]:
                st.markdown(f'<div class="choice-card"><span>Current option</span><h3>{esc(route.get("name", "Travel route"))}</h3>'
                            f'<p>{esc(route.get("description", "Check live fare and availability"))}</p></div>', unsafe_allow_html=True)
                render_item_links(report, route.get("source_ids", []))
                if st.button("Choose this route", key=f"{thread_id}_route_{pos}", use_container_width=True):
                    save_journey(thread_id, report, {"travel_route": route}, complete="travel")
                    st.rerun()
    render_agent_steps(thread_id, report, "travel")
    render_stage_sources(report, "travel")


def ready_to_travel(report: dict) -> None:
    profile = report["profile"]
    section("You are ready to travel", "Keep these essentials together in your hand luggage.")
    documents = ["Passports and visas", "Employment and accommodation details",
                 "Travel tickets", "Insurance documents", "Family medical and school records",
                 "Shipping inventory and mover contact"]
    st.markdown('<div class="document-grid">' + "".join(
        f'<div><span>✓</span>{esc(item)}</div>' for item in documents) + '</div>', unsafe_allow_html=True)
    st.info(f"Your plan will reopen here for the move to {profile['destination']}.")


def render_choice_summary(report: dict) -> None:
    """Show one customer-readable record of decisions made across the journey."""
    profile = report["profile"]
    completed = set(profile.get("completed_steps", []))
    currency_code, currency_symbol = move_currency(profile)
    home = profile.get("preferred_home") or {}
    home_name = profile.get("preferred_home_name") or "Not selected yet"
    rent_value = home.get("monthly_krw") or home.get("rent")
    rent = (f"{currency_symbol}{int(rent_value):,} {currency_code} per month"
            if rent_value else "Rent still to be confirmed")
    visa = profile.get("visa_route") or "Not selected yet"
    finance_needs = profile.get("finance_needs") or []
    finance = ", ".join(finance_needs) if finance_needs else "Preferences not added yet"
    shipping_items = profile.get("shipping_items") or []
    movers = profile.get("shipping_requests") or []
    shipping = ", ".join(shipping_items) if shipping_items else "No items selected yet"
    mover_note = f"Mover requests: {', '.join(movers)}" if movers else "No mover contacted yet"
    route = profile.get("travel_route") or {}
    route_name = route.get("name") if isinstance(route, dict) else str(route)
    travel = route_name or "Not selected yet"

    cards = [
        ("01", "Visa path", visa, "immigration"),
        ("02", "Chosen home", home_name, "housing", rent),
        ("03", "Money setup", finance, "finance"),
        ("04", "Things to move", shipping, "logistics", mover_note),
        ("05", "Travel route", travel, "travel",
         f"{profile.get('travel_cabin', 'Cabin not selected')} · {profile.get('travel_bags', 0)} checked bags"),
    ]
    st.markdown('<div class="choice-summary"><span class="summary-kicker">YOUR SAVED CHOICES</span>'
                '<h2>Your relocation plan in one view</h2>'
                f'<p>{esc(profile["origin"])} → {esc(profile["destination"])} · '
                f'{esc(profile["move_date"])} · {int(profile.get("family_size", 1))} people</p></div>',
                unsafe_allow_html=True)
    summary_columns = [*st.columns(3), *st.columns(2)]
    for column, (number, title, choice, stage, *detail) in zip(summary_columns, cards):
        status = "Complete" if stage in completed else "In progress" if choice not in {
            "Not selected yet", "Preferences not added yet", "No items selected yet"} else "To do"
        column.markdown(f'<div class="summary-card"><div class="summary-card-top"><i>{number}</i>'
                        f'<span class="summary-status {status.lower().replace(" ", "-")}">{status}</span></div>'
                        f'<small>{esc(title)}</small><strong>{esc(choice)}</strong>'
                        f'{f"<p>{esc(detail[0])}</p>" if detail else ""}</div>', unsafe_allow_html=True)
        with column.popover("View details", use_container_width=True):
            st.markdown(f"**{title}**")
            if stage == "immigration":
                st.write(f"**Selected path:** {visa}")
                st.write(f"**Citizenship:** {profile.get('citizenships') or 'Not added'}")
                st.write(f"**Work situation:** {profile.get('employment_path') or 'Not added'}")
                st.write("**Dependent visas:** " + ("Needed" if profile.get("dependent_visas") else "Not requested"))
            elif stage == "housing":
                st.write(f"**Home:** {home_name}")
                st.write(f"**Rent:** {rent}")
                st.write(f"**Office area:** {profile.get('workplace') or 'Not added'}")
                st.write(f"**School area:** {profile.get('school_area') or 'Not added'}")
                st.write(f"**Maximum commute:** {profile.get('max_daycare_commute_min') or 'Not set'} minutes")
                if profile.get("home_preferences"):
                    st.write("**Home preferences:** " + ", ".join(profile["home_preferences"]))
            elif stage == "finance":
                st.write("**Help requested:** " + finance)
                transfer = profile.get("monthly_transfer")
                st.write(f"**Expected monthly transfer:** {currency_symbol}{int(transfer):,} {currency_code}"
                         if transfer else "**Expected monthly transfer:** Not added")
            elif stage == "logistics":
                st.write("**Items:** " + shipping)
                st.write("**Movers contacted:** " + (", ".join(movers) if movers else "None yet"))
                if profile.get("shipping_notes"):
                    st.write("**Instructions:** " + str(profile["shipping_notes"]))
            else:
                st.write("**Selected route:** " + travel)
                if isinstance(route, dict) and route.get("description"):
                    st.write(str(route["description"]))
                st.write(f"**Cabin:** {profile.get('travel_cabin') or 'Not selected'}")
                st.write(f"**Stops:** {profile.get('travel_stops') or 'Not selected'}")
                st.write(f"**Checked bags:** {profile.get('travel_bags', 0)}")
                if profile.get("travel_priorities"):
                    st.write("**Priorities:** " + ", ".join(profile["travel_priorities"]))
            finding = agent_finding(report, stage)
            guide_steps = finding.get("steps", [])
            finished_steps = profile.get(f"{stage}_guide_steps", [])
            if guide_steps:
                st.caption(f"{len(finished_steps)} of {len(guide_steps)} guidance steps marked complete")
            source_ids = [item["id"] for item in report.get("evidence", []) if item.get("domain") == stage]
            render_item_links(report, source_ids)


def render_saved_move(thread_id: str) -> None:
    try:
        state = load_move(thread_id)
        report = state.get("report", {})
    except Exception:
        error_message("Open this move", thread_id)
        return
    if not report:
        st.info("This move is still being prepared. Please refresh in a moment.")
        return
    profile = report["profile"]
    if "next_actions" not in report and any(source["domain"] == "housing" for source in report.get("evidence", [])):
        key = f"{thread_id}_handoff_attempted"
        if not st.session_state.get(key):
            st.session_state[key] = True
            try:
                with st.spinner("Connecting your home search to the rest of your move..."):
                    update_move(thread_id, profile, report["evidence"])
                st.rerun()
            except Exception:
                error_message("Refresh your next steps", thread_id)
    complete_steps = set(profile.get("completed_steps", []))
    ready = sum(step in complete_steps for step, _, _ in JOURNEY_STEPS)
    days_left = (date.fromisoformat(profile["move_date"]) - date.today()).days
    countdown = f"{days_left} days to go" if days_left >= 0 else "Move date passed"
    st.markdown(f'<div class="dashboard-intro"><div><span class="dashboard-kicker">YOUR MOVE AT A GLANCE</span>'
                f'<h2>{esc(countdown)}</h2><p>{esc(profile["origin"])} to {esc(profile["destination"])}'
                f' &middot; {int(profile.get("family_size", 1))} people</p></div>'
                f'<div class="dashboard-progress"><strong>{ready}/{len(JOURNEY_STEPS)}</strong>'
                f'<span>stages complete</span></div></div>', unsafe_allow_html=True)
    current = journey_header(profile)
    with st.container(border=True):
        st.markdown('<span class="stage-workspace-anchor"></span>', unsafe_allow_html=True)
        if current == "immigration":
            visa_stage(thread_id, report)
        elif current == "housing":
            housing_stage(thread_id, report)
        elif current == "finance":
            finance_stage(thread_id, report)
        elif current == "logistics":
            logistics_stage(thread_id, report)
        elif current == "travel":
            travel_stage(thread_id, report)
        else:
            ready_to_travel(report)
    render_choice_summary(report)
    st.markdown('<div class="plan-tools-title">Plan controls</div>', unsafe_allow_html=True)
    tool_cols = st.columns(3)
    with tool_cols[0]:
        render_details(thread_id, report)
    if tool_cols[1].button("Plan another move", key=f"{thread_id}_start_different", use_container_width=True):
        st.session_state.starting_new_relocation = True
        st.session_state.thread_id = None
        st.rerun()
    tool_cols[2].download_button("Download plan", plan_text(report),
                                 f"my-move-{thread_id[:8]}.txt", "text/plain", use_container_width=True)


runs = index.list(USER_ID)
brand_col, reset_col = st.columns([6, 1.1], vertical_alignment="center")
brand_col.markdown('<div class="brand"><span class="brand-mark">m.</span><span>movewell'
                   '<small>Every part of your relocation, in one place</small></span></div>', unsafe_allow_html=True)
if runs:
    with reset_col.popover("Plan menu", use_container_width=True):
        st.caption("Manage the relocation saved on this device.")
        if st.button("Reset and start over", key="reset_relocation", use_container_width=True):
            try:
                index.clear(USER_ID)
                # Keep Streamlit's widget bookkeeping intact during this button event.
                # The next run naturally discards move-scoped keys with the old thread id.
                st.session_state.thread_id = None
                st.session_state.starting_new_relocation = False
                st.rerun()
            except Exception:
                error_message("Reset this relocation")
active = st.session_state.get("thread_id")
known_ids = {run["thread_id"] for run in runs}
if st.session_state.get("starting_new_relocation"):
    thread_id = None
elif active in known_ids:
    thread_id = active
else:
    thread_id = runs[0]["thread_id"] if runs else None
st.session_state.thread_id = thread_id
if thread_id:
    current = next(run for run in runs if run["thread_id"] == thread_id)
    hero_class = "hero move-hero scenic" if "seoul" in current["destination"].lower() else "hero move-hero"
    hero = f'<div class="{hero_class}"><div class="hero-content"><div class="eyebrow">YOUR MOVE</div>'
    hero += f'<h1>{esc(current["origin"])} to {esc(current["destination"])}</h1>'
    hero += '<p>Your home leads, decisions and next steps are all here.</p></div></div>'
else:
    hero = '<div class="hero scenic"><div class="hero-content"><div class="eyebrow">MOVE SMARTER, ARRIVE READY</div>'
    hero += '<h1>Where will life take you next?</h1><p>Plan the journey from your first decision to moving day.</p></div></div>'
st.markdown(hero, unsafe_allow_html=True)
if thread_id:
    render_saved_move(thread_id)
else:
    render_new_move()




