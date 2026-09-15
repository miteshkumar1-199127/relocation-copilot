from __future__ import annotations

from datetime import date
from typing import Any, Literal, TypedDict
from urllib.parse import urlparse


class Evidence(TypedDict):
    id: str
    domain: str
    title: str
    url: str
    retrieved_at: str
    text: str


class Task(TypedDict):
    id: str
    title: str
    depends_on: list[str]
    status: Literal["ready", "working", "blocked", "needs_user", "done", "failed"]
    reason: str


class State(TypedDict, total=False):
    user_id: str
    profile: dict[str, Any]
    evidence: list[Evidence]
    memory: list[str]
    findings: dict[str, Any]
    tasks: list[Task]
    questions: list[str]
    conflicts: list[str]
    errors: list[str]
    report: dict[str, Any]
    approval: dict[str, Any]
    recommendations: list[dict[str, Any]]


DOMAINS = ("immigration", "housing", "finance", "family", "logistics", "travel")


def validate_profile(profile: dict[str, Any]) -> dict[str, Any]:
    result = dict(profile)
    for key in ("origin", "destination", "move_date"):
        if not str(result.get(key, "")).strip():
            raise ValueError(f"{key} is required")
    try:
        result["move_date"] = date.fromisoformat(str(result["move_date"])).isoformat()
    except ValueError as exc:
        raise ValueError("move_date must be YYYY-MM-DD") from exc
    for key in ("housing_budget_krw", "max_daycare_commute_min"):
        if result.get(key) not in (None, ""):
            result[key] = int(result[key])
            if result[key] <= 0:
                raise ValueError(f"{key} must be positive")
    result["family_size"] = int(result.get("family_size", 1))
    if result["family_size"] < 1 or result["family_size"] > 20:
        raise ValueError("family_size must be between 1 and 20")
    return result


def validate_evidence(items: list[Evidence]) -> list[Evidence]:
    ids: set[str] = set()
    clean: list[Evidence] = []
    for item in items:
        source_id = str(item.get("id", "")).strip()
        url = str(item.get("url", "")).strip()
        parsed = urlparse(url)
        if not source_id or source_id in ids:
            raise ValueError("Evidence IDs must be nonempty and unique")
        if item.get("domain") not in DOMAINS:
            raise ValueError(f"Invalid evidence domain: {item.get('domain')}")
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"Evidence must have an HTTPS URL: {source_id}")
        if not str(item.get("text", "")).strip():
            raise ValueError(f"Evidence text is empty: {source_id}")
        ids.add(source_id)
        clean.append({
            "id": source_id, "domain": item["domain"], "title": str(item.get("title") or parsed.hostname),
            "url": url, "retrieved_at": str(item.get("retrieved_at", "")),
            "text": str(item["text"])[:16000],
        })
    return clean
