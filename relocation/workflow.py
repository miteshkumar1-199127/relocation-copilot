"""Stateful specialist workflow, conflict resolution, and approval boundary."""

from __future__ import annotations

import sqlite3
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .config import Settings
from .models import DOMAINS, Evidence, State, Task, validate_evidence, validate_profile
from .providers import Mem0Store, Nebius


def _task(task_id: str, title: str, deps: list[str], status: str, reason: str = "") -> Task:
    return {"id": task_id, "title": title, "depends_on": deps, "status": status, "reason": reason}  # type: ignore[return-value]


def _required_questions(profile: dict[str, Any]) -> list[str]:
    questions = []
    for key, question in [
        ("citizenships", "What citizenship and passport details apply to each traveler?"),
        ("visa_status", "What is each traveler's current visa or residence status?"),
        ("workplace", "Where will the workplace be?"),
        ("housing_budget_krw", "What is your monthly housing budget in KRW?"),
    ]:
        if not profile.get(key):
            questions.append(question)
    if profile.get("family_size", 1) > 1 and not profile.get("family_needs"):
        questions.append("What daycare, school, or healthcare needs should the plan account for?")
    return questions


def next_actions(report: dict[str, Any]) -> list[dict[str, str]]:
    """Turn specialist results into an actionable, source-aware handoff."""
    profile = report["profile"]
    recommendations = report.get("recommendations", [])
    preferred = profile.get("preferred_home_name")
    lead = next((item for item in recommendations if item["candidate"].get("name") == preferred),
                recommendations[0] if recommendations else None)
    actions: list[dict[str, str]] = []
    if lead:
        candidate = lead["candidate"]
        name = str(candidate.get("name") or "your home lead")
        rent = candidate.get("monthly_krw")
        actions.append({"id": "housing_choice", "title": "Review your home lead",
                        "detail": f"Check availability, deposit and lease terms for {name} with the housing source.",
                        "status": "chosen" if preferred == name else "choose"})
        if rent is not None:
            budget = profile.get("housing_budget_krw")
            detail = (f"Compare ₩{int(rent):,} monthly rent with your ₩{int(budget):,} budget; confirm the deposit and other costs."
                      if budget else f"Monthly rent is listed as ₩{int(rent):,}. Add your budget, then check deposit and other costs.")
        else:
            detail = "Ask the housing source for the monthly rent and deposit before comparing costs."
        actions.append({"id": "finance", "title": "Check the full housing cost", "detail": detail,
                        "status": "ready"})
        if profile.get("family_size", 1) > 1:
            actions.append({"id": "family", "title": "Check the family commute",
                            "detail": f"Check daycare or school options and travel time from {name}; the listing alone cannot confirm this.",
                            "status": "ready"})
        furnished = candidate.get("furnished")
        shipping = ("The home is listed as furnished. Confirm what is included before choosing what to ship."
                    if furnished is True else "Confirm what the home includes, then decide what to ship or buy.")
        actions.append({"id": "logistics", "title": "Plan what to bring", "detail": shipping,
                        "status": "ready"})
    else:
        actions.append({"id": "housing_choice", "title": "Find a home lead",
                        "detail": "Add a housing source or adjust the limits that ruled out the current listings.",
                        "status": "waiting"})
    if not profile.get("citizenships") or not profile.get("visa_status"):
        visa_detail = "Add each traveler's citizenship and current visa situation before checking official requirements."
    else:
        visa_detail = "Check the current entry and residence requirements with an official source before booking travel."
    actions.append({"id": "immigration", "title": "Confirm documents and visas",
                    "detail": visa_detail, "status": "ready"})
    actions.append({"id": "travel", "title": "Prepare the journey",
                    "detail": "Compare travel options for your move date after the document and visa plan is confirmed.",
                    "status": "waiting"})
    return actions


def _candidate_ok(candidate: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons = []
    rent = candidate.get("monthly_krw")
    commute = candidate.get("daycare_commute_min")
    budget = profile.get("housing_budget_krw")
    limit = profile.get("max_daycare_commute_min")
    if rent is None or budget is None:
        reasons.append("Rent or housing budget is unknown")
    else:
        try:
            if int(rent) > int(budget):
                reasons.append("Monthly rent exceeds budget")
        except (TypeError, ValueError):
            reasons.append("Rent is not a valid number")
    if profile.get("family_needs") and limit is not None:
        if commute is None:
            reasons.append("Daycare commute is unknown")
        else:
            try:
                if int(commute) > int(limit):
                    reasons.append("Daycare commute exceeds limit")
            except (TypeError, ValueError):
                reasons.append("Daycare commute is not a valid number")
    return not reasons, reasons


class UnavailableMemory:
    """Allow planning to continue when preference storage cannot start."""

    def __init__(self, reason: str):
        self.reason = reason

    def recall(self, user_id: str, query: str) -> list[str]:
        raise RuntimeError(self.reason)

    def remember(self, user_id: str, text: str) -> None:
        raise RuntimeError(self.reason)


class RelocationWorkflow:
    def __init__(self, settings: Settings, llm: Nebius | None = None, memory: Mem0Store | None = None):
        settings.prepare()
        self.settings = settings
        self.llm = llm or Nebius(settings)
        if memory is not None:
            self.memory = memory
        else:
            try:
                self.memory = Mem0Store(settings)
            except Exception as exc:
                self.memory = UnavailableMemory(str(exc))
        checkpoint_path = settings.data_dir / "checkpoints.sqlite" if settings.persist_data else ":memory:"
        self.connection = sqlite3.connect(checkpoint_path, timeout=30,
                                          check_same_thread=False)
        self.graph = self._build()

    def _load_memory(self, state: State) -> dict[str, Any]:
        try:
            facts = self.memory.recall(state["user_id"], "relocation preferences budget family commute")
            return {"memory": facts}
        except Exception as exc:
            return {"memory": [], "errors": [f"Memory retrieval failed: {exc}"]}

    def _plan(self, state: State) -> dict[str, Any]:
        p = state["profile"]
        evidence_domains = {e["domain"] for e in state["evidence"]}
        questions = _required_questions(p)
        tasks = [_task("profile", "Confirm family and move details", [], "needs_user" if questions else "done")]
        for domain in DOMAINS:
            # Immigration research helps the user choose a visa status, so it
            # must not wait for that same answer in the profile.
            deps = []
            if domain in ("family", "finance", "logistics"):
                deps = ["housing"]
            if domain == "travel":
                deps = ["immigration"]
            tasks.append(_task(domain, f"Research {domain}", deps,
                               "ready" if domain in evidence_domains else "blocked",
                               "" if domain in evidence_domains else "Add a dated source"))
        return {"questions": questions, "tasks": tasks, "findings": {}, "conflicts": [],
                "errors": state.get("errors", [])}

    def _specialist(self, role: str):
        def node(state: State) -> dict[str, Any]:
            sources = [e for e in state["evidence"] if e["domain"] == role]
            findings = dict(state.get("findings", {}))
            tasks = [dict(t) for t in state["tasks"]]
            current = next(t for t in tasks if t["id"] == role)
            unmet = [dep for dep in current["depends_on"]
                     if next(t for t in tasks if t["id"] == dep)["status"] != "done"]
            if unmet:
                current["status"] = "blocked"
                current["reason"] = "Waiting for " + ", ".join(unmet)
                findings[role] = {"summary": current["reason"], "actions": [],
                                  "questions": [], "claims": [], "candidates": []}
                return {"findings": findings, "tasks": tasks}
            if not sources:
                handoff = next_actions({"profile": state["profile"],
                                        "recommendations": state.get("recommendations", [])})
                relevant = next((item for item in handoff if item["id"] == role), None)
                current["status"] = "needs_user" if relevant else "blocked"
                current["reason"] = "Needs a trusted source to verify details" if relevant else "Add a dated source"
                findings[role] = {"summary": relevant["detail"] if relevant else "No source supplied",
                                  "actions": [relevant["detail"]] if relevant else [],
                                  "questions": [f"Add a {role} source"], "claims": [], "candidates": []}
                return {"findings": findings, "tasks": tasks}
            instructions = ""
            if role == "housing":
                instructions = "Extract up to ten concrete housing candidates and their exact sourced rent, commute, furnished status. Null for missing fields."
            payload = {"profile": state["profile"], "memory": state.get("memory", []),
                       "evidence": sources, "prior_findings": findings, "instructions": instructions}
            try:
                findings[role] = self.llm.json(role, payload, {e["id"] for e in sources})
                for task in tasks:
                    if task["id"] == role:
                        task["status"] = "done"
                        task["reason"] = "Research completed; review source claims"
                return {"findings": findings, "tasks": tasks}
            except Exception as exc:
                findings[role] = {"summary": "Research failed", "actions": [], "questions": [f"Retry {role} research"], "claims": [], "candidates": []}
                for task in tasks:
                    if task["id"] == role:
                        task["status"] = "failed"
                        task["reason"] = str(exc)
                return {"findings": findings, "tasks": tasks,
                        "errors": state.get("errors", []) + [f"{role}: {exc}"]}
        return node

    def _reconcile(self, state: State) -> dict[str, Any]:
        candidates = state.get("findings", {}).get("housing", {}).get("candidates", [])
        conflicts = []
        recommendations = []
        profile = state["profile"]
        for candidate in candidates:
            okay, reasons = _candidate_ok(candidate, profile)
            hard_conflicts = [reason for reason in reasons if reason in (
                "Monthly rent exceeds budget", "Daycare commute exceeds limit", "Rent is not a valid number",
                "Daycare commute is not a valid number")]
            if hard_conflicts:
                conflicts.append(f"{candidate.get('name', 'Unnamed home')}: {', '.join(reasons)}")
            else:
                recommendations.append({"candidate": candidate,
                                        "status": "verified" if okay else "needs_check",
                                        "caveats": reasons})
        preferred = profile.get("preferred_home_name")
        recommendations.sort(key=lambda item: (item["status"] != "verified",
                                                  item["candidate"].get("name") != preferred if preferred else False,
                                                  int(item["candidate"].get("monthly_krw") or 10**18)))
        selection = next((item["candidate"] for item in recommendations if item["status"] == "verified"), None)
        findings = dict(state.get("findings", {}))
        findings["selection"] = {"candidate": selection, "rejected": conflicts}
        questions = list(state.get("questions", []))
        if not recommendations:
            questions.append("No fully verified housing candidate meets all known constraints. Add sources or revise constraints.")
        return {"findings": findings, "conflicts": conflicts, "questions": questions,
                "recommendations": recommendations[:5]}

    def _critic(self, state: State) -> dict[str, Any]:
        ids = {e["id"] for e in state["evidence"]}
        issues = list(state.get("errors", []))
        for task in state["tasks"]:
            if task["status"] in ("blocked", "failed", "needs_user"):
                issues.append(f"{task['id']}: {task['status']} — {task['reason'] or 'user input required'}")
        for role, result in state.get("findings", {}).items():
            for claim in result.get("claims", []):
                if not set(claim.get("source_ids", [])).issubset(ids):
                    issues.append(f"{role} contains an invalid citation")
        if not state.get("evidence"):
            issues.append("No evidence has been supplied")
        for source in state.get("evidence", []):
            if not source.get("retrieved_at"):
                issues.append(f"Source {source['id']} lacks retrieval time")
        if state.get("questions"):
            issues.append("Open user questions remain")
        selection = state.get("findings", {}).get("selection", {}).get("candidate")
        if not selection:
            issues.append("No housing candidate is ready for approval")
        status = "needs_sources" if not state.get("evidence") else "incomplete" if issues else "needs_review"
        report = {"profile": state["profile"], "tasks": state["tasks"],
                  "findings": state["findings"], "selection": selection,
                  "recommendations": state.get("recommendations", []),
                  "conflicts": state.get("conflicts", []), "questions": state.get("questions", []),
                  "issues": issues, "evidence": state["evidence"],
                  "status": status}
        report["next_actions"] = next_actions(report)
        return {"report": report}

    def _approval(self, state: State) -> dict[str, Any]:
        selection = state["report"].get("selection")
        decision = interrupt({"type": "plan_review", "report": state["report"],
                              "message": "Approve this plan for your own use? No external action will be performed."})
        if not isinstance(decision, dict) or decision.get("decision") not in ("approve", "reject"):
            raise ValueError("Decision must be approve or reject")
        return {"approval": {"decision": decision["decision"],
                             "note": str(decision.get("note", ""))[:1000],
                             "candidate": selection.get("name") if selection else None}}

    def _build(self):
        builder = StateGraph(State)
        builder.add_node("load_memory", self._load_memory)
        builder.add_node("plan", self._plan)
        for role in DOMAINS:
            builder.add_node(role, self._specialist(role))
        builder.add_node("reconcile", self._reconcile)
        builder.add_node("critic", self._critic)
        builder.add_node("approval", self._approval)
        order = ["load_memory", "plan", "immigration", "housing", "reconcile",
                 "finance", "family", "logistics", "travel", "critic", "approval"]
        builder.add_edge(START, order[0])
        for a, b in zip(order, order[1:]):
            builder.add_edge(a, b)
        builder.add_edge(order[-1], END)
        return builder.compile(checkpointer=SqliteSaver(self.connection))

    def start(self, thread_id: str, user_id: str, profile: dict[str, Any], evidence: list[Evidence]) -> State:
        profile = validate_profile(profile)
        evidence = validate_evidence(evidence)
        config = {"configurable": {"thread_id": thread_id}}
        return self.graph.invoke({"user_id": user_id, "profile": profile, "evidence": evidence,
                                  "errors": [], "approval": {}}, config=config)

    def resume(self, thread_id: str, decision: str, note: str = "") -> State:
        config = {"configurable": {"thread_id": thread_id}}
        return self.graph.invoke(Command(resume={"decision": decision, "note": note}), config=config)

    def get(self, thread_id: str) -> State:
        config = {"configurable": {"thread_id": thread_id}}
        return self.graph.get_state(config).values
