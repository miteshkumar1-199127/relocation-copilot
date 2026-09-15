"""Run end-to-end LangSmith experiments against the real Nebius workflow."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from langsmith import Client

from relocation.config import Settings
from relocation.workflow import RelocationWorkflow


EVIDENCE = [{
    "id": "fixture-housing-1", "domain": "housing", "title": "Synthetic housing fixture",
    "url": "https://example.org/relocation-eval-fixture",
    "retrieved_at": "2026-09-13T00:00:00Z",
    "text": (
        "SYNTHETIC EVALUATION DATA, NOT A REAL LISTING. Apartment A: monthly rent KRW 5,000,000; "
        "daycare commute 20 minutes; furnished. Apartment B: monthly rent KRW 3,500,000; "
        "daycare commute 45 minutes; unfurnished. Apartment C: monthly rent KRW 3,900,000; "
        "daycare commute 25 minutes; furnished. All are fictional examples."
    ),
}]

BASE_PROFILE = {
    "origin": "Bangalore", "destination": "Seoul", "move_date": "2026-10-24",
    "family_size": 3, "citizenships": "Indian", "visa_status": "Needs verification",
    "workplace": "Seoul", "family_needs": "Toddler daycare",
    "housing_budget_krw": 4_000_000, "max_daycare_commute_min": 30,
}

CASES = [
    ("balanced", 4_000_000, 30, "C"),
    ("low_budget", 3_000_000, 30, None),
    ("high_budget", 5_500_000, 22, "A"),
    ("strict_commute", 4_000_000, 20, None),
    ("no_daycare_constraint", 4_000_000, 60, "B"),
]


def main() -> None:
    settings = Settings()
    settings.validate_live()
    if not settings.langsmith_key:
        raise RuntimeError("LANGSMITH_API_KEY is required")
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    workflow = RelocationWorkflow(settings)
    client = Client()
    name = "relocation-copilot-housing-v1"
    try:
        dataset = client.read_dataset(dataset_name=name)
    except Exception:
        dataset = client.create_dataset(dataset_name=name,
                                        description="Synthetic housing conflict and approval scenarios")
        client.create_examples(
            dataset_id=dataset.id,
            inputs=[{"profile": {**BASE_PROFILE, "housing_budget_krw": budget,
                                   "max_daycare_commute_min": commute}, "evidence": EVIDENCE}
                    for _, budget, commute, _ in CASES],
            outputs=[{"expected_home": expected} for _, _, _, expected in CASES],
        )

    def target(inputs: dict) -> dict:
        result = workflow.start("eval-" + uuid.uuid4().hex, "eval-" + uuid.uuid4().hex,
                                inputs["profile"], inputs["evidence"])
        report = result["report"]
        return {"selected_home": (report["selection"] or {}).get("name"),
                "approval_pause": bool(result.get("__interrupt__")),
                "invalid_claims": [issue for issue in report["issues"] if "citation" in issue],
                "questions": report["questions"], "conflicts": report["conflicts"]}

    def selection(outputs: dict, reference_outputs: dict) -> dict:
        actual = outputs["selected_home"]
        if isinstance(actual, str):
            actual = actual.strip().removeprefix("Apartment ").strip()
        return {"key": "correct_housing_selection",
                "score": int(actual == reference_outputs["expected_home"])}

    def human_review(outputs: dict) -> dict:
        return {"key": "human_review_pause", "score": int(outputs["approval_pause"])}

    def citations(outputs: dict) -> dict:
        return {"key": "valid_citations", "score": int(not outputs["invalid_claims"])}

    result = client.evaluate(target, data=name,
                             evaluators=[selection, human_review, citations],
                             experiment_prefix="relocation-copilot", max_concurrency=1)
    print(result)


if __name__ == "__main__":
    main()
