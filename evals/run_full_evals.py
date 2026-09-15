"""Run the versioned Movewell golden set in LangSmith and write aggregate metrics locally."""

from __future__ import annotations

import json
import os
import statistics
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langsmith import Client

from relocation.config import Settings
from relocation.providers import Nebius
from relocation.workflow import RelocationWorkflow


ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "evals" / "golden_dataset.json"
METRICS_JSON = ROOT / "evals" / "latest_metrics.json"
METRICS_MD = ROOT / "evals" / "EVALUATION_REPORT.md"
REQUIRED_KEYS = {"summary", "actions", "questions", "claims", "candidates", "options", "steps"}


class NoMemory:
    def recall(self, user_id: str, query: str) -> list[str]:
        return []

    def remember(self, user_id: str, text: str) -> None:
        return None


def normalize_home(value: Any) -> str | None:
    if value is None:
        return None
    return str(value).strip().removeprefix("Apartment ").strip()


def cited_items(finding: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for key in ("claims", "candidates", "options", "steps"):
        items.extend(item for item in finding.get(key, []) if isinstance(item, dict))
    return items


def score_case(case: dict[str, Any], output: dict[str, Any]) -> dict[str, float | None]:
    expected = case["expected"]
    if output.get("error"):
        return {"error_free": 0.0, "schema_valid": 0.0, "citation_grounding": 0.0,
                "useful_output": 0.0, "expected_content": 0.0,
                "housing_selection": 0.0 if case["case_type"] == "workflow" else None,
                "human_review_pause": 0.0 if case["case_type"] == "workflow" else None,
                "within_45_seconds": 0.0}
    finding = output.get("finding", {})
    schema = REQUIRED_KEYS.issubset(finding) if case["case_type"] == "specialist" else bool(output.get("report"))
    allowed = {item["id"] for item in case["evidence"]}
    items = cited_items(finding)
    grounded = (sum(bool(item.get("source_ids")) and set(item["source_ids"]).issubset(allowed)
                    for item in items) / len(items)) if items else 0.0
    useful = (len(finding.get("options", [])) >= expected.get("min_options", 0) and
              len(finding.get("steps", [])) >= expected.get("min_steps", 0) and
              len(finding.get("candidates", [])) >= expected.get("min_candidates", 0))
    rendered = json.dumps(output, ensure_ascii=False).casefold()
    expected_content = all(term.casefold() in rendered for term in expected.get("required_terms", []))
    workflow_case = case["case_type"] == "workflow"
    return {
        "error_free": 1.0,
        "schema_valid": float(schema),
        "citation_grounding": grounded if case["case_type"] == "specialist" else None,
        "useful_output": float(useful),
        "expected_content": float(expected_content),
        "housing_selection": float(normalize_home(output.get("selection")) == normalize_home(expected.get("selection"))) if workflow_case else None,
        "human_review_pause": float(bool(output.get("approval_pause")) == bool(expected.get("approval_pause"))) if workflow_case else None,
        "within_45_seconds": float(output.get("latency_ms", 10**9) <= 45000),
    }


def main() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = golden["cases"]
    settings = Settings()
    settings.validate_live()
    if not settings.langsmith_key:
        raise RuntimeError("LANGSMITH_API_KEY is required")
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    llm = Nebius(settings, settings.research_model)
    eval_settings = replace(settings, persist_data=False)
    workflow = RelocationWorkflow(eval_settings, llm=llm, memory=NoMemory())
    observed: dict[str, dict[str, Any]] = {}

    def target(inputs: dict[str, Any]) -> dict[str, Any]:
        case = inputs
        started = time.perf_counter()
        try:
            if case["case_type"] == "workflow":
                result = workflow.start("golden-" + uuid.uuid4().hex, "golden-eval",
                                        case["profile"], case["evidence"])
                report = result["report"]
                output = {"case_id": case["id"], "case_type": case["case_type"],
                          "selection": (report.get("selection") or {}).get("name"),
                          "approval_pause": bool(result.get("__interrupt__")), "report": report,
                          "finding": report.get("findings", {}).get("housing", {})}
            else:
                finding = llm.json(case["role"], {"profile": case["profile"],
                    "evidence": case["evidence"], "instructions": "Return concise, practical, fully sourced output."},
                    {item["id"] for item in case["evidence"]},
                    max_tokens={"housing": 900, "finance": 1100}.get(case["role"], 1400))
                output = {"case_id": case["id"], "case_type": case["case_type"], "finding": finding}
        except Exception as exc:
            output = {"case_id": case["id"], "case_type": case["case_type"],
                      "error": f"{type(exc).__name__}: {exc}"}
        output["latency_ms"] = round((time.perf_counter() - started) * 1000, 1)
        observed[case["id"]] = output
        return output

    def evaluator(metric: str):
        def evaluate(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
            case = {**inputs, "expected": reference_outputs}
            value = score_case(case, outputs)[metric]
            return {"key": metric, "score": value, "comment": "Not applicable" if value is None else "Golden-set metric"}
        return evaluate

    client = Client()
    try:
        dataset = client.read_dataset(dataset_name=golden["name"])
    except Exception:
        dataset = client.create_dataset(dataset_name=golden["name"], description=golden["description"])
        client.create_examples(
            dataset_id=dataset.id,
            inputs=[{key: value for key, value in case.items() if key != "expected"} for case in cases],
            outputs=[case["expected"] for case in cases],
        )
    experiment = client.evaluate(
        target, data=golden["name"],
        evaluators=[evaluator(metric) for metric in ("error_free", "schema_valid", "citation_grounding",
                    "useful_output", "expected_content", "housing_selection",
                    "human_review_pause", "within_45_seconds")],
        experiment_prefix="movewell-full-golden", max_concurrency=1,
    )
    list(experiment)

    case_rows = []
    metric_values: dict[str, list[float]] = {}
    for case in cases:
        output = observed.get(case["id"], {"error": "No output", "latency_ms": 0})
        scores = score_case(case, output)
        case_rows.append({"id": case["id"], "role": case["role"], "type": case["case_type"],
                          "latency_ms": output.get("latency_ms"), "error": output.get("error"),
                          "scores": scores})
        for metric, value in scores.items():
            if value is not None:
                metric_values.setdefault(metric, []).append(float(value))
    latencies = [float(row["latency_ms"]) for row in case_rows]
    aggregates = {metric: round(sum(values) / len(values), 4) for metric, values in metric_values.items()}
    aggregates["latency_p50_ms"] = round(statistics.median(latencies), 1)
    aggregates["latency_p95_ms"] = round(sorted(latencies)[max(0, int(len(latencies) * .95) - 1)], 1)
    payload = {"dataset": golden["name"], "run_at": datetime.now(timezone.utc).isoformat(),
               "experiment": str(experiment), "cases": len(cases), "aggregates": aggregates,
               "case_results": case_rows}
    METRICS_JSON.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = ["# Movewell full evaluation report", "", f"- Dataset: `{golden['name']}`",
             f"- Run time: {payload['run_at']}", f"- Golden cases: {len(cases)}", "", "## Aggregate metrics", "",
             "| Metric | Result |", "|---|---:|"]
    for metric, value in aggregates.items():
        display = f"{value * 100:.1f}%" if not metric.startswith("latency_") else f"{value:,.0f} ms"
        lines.append(f"| {metric.replace('_', ' ').title()} | {display} |")
    lines += ["", "## Case results", "", "| Case | Agent | Type | Latency | Error |", "|---|---|---|---:|---|"]
    for row in case_rows:
        lines.append(f"| {row['id']} | {row['role']} | {row['type']} | {row['latency_ms']:,.0f} ms | {row['error'] or 'None'} |")
    lines += ["", "All evidence in this dataset is synthetic. These metrics test grounded structure, control flow, and constraints; they do not certify real-world visa, financial, housing, or travel accuracy."]
    METRICS_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(json.dumps(aggregates, indent=2))


if __name__ == "__main__":
    main()
