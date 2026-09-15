# Relocation Copilot

A single-owner relocation planning app with a customer-facing interface called **Movewell**. LangGraph coordinates six backend specialists and a verifier across five customer stages, Nebius performs model and embedding calls, Mem0 retains user preferences, and LangSmith records traces and evaluation experiments. The app retrieves dated web evidence through You.com, reconciles housing against budget and commute, persists progress in SQLite, and pauses for human choices. It never books, pays, uploads, or sends messages.

## Start locally

1. Create a Python 3.12 or 3.13 virtual environment and install `requirements.txt`.
2. Copy `.env.example` to `.env`. Set `NEBIUS_API_KEY`, a currently available `NEBIUS_MODEL`, `LANGSMITH_API_KEY`, and `YOU_API_KEY`. `NEBIUS_EMBED_MODEL` defaults to `Qwen/Qwen3-Embedding-8B`. You.com Search supplies current sources for the active specialist stage.
3. Run `streamlit run app.py` and open the local URL shown by Streamlit.
4. Start a move from the home page. The workspace guides the user through five stages in order: visa, home, finances, shipping, and travel. Each stage contains its questions, choices, actions, and completion checkpoint. When the journey is complete, the workspace becomes a travel-document checklist.

PowerShell example:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\streamlit.exe run app.py
```

Persistence is enabled with `PERSIST_RELOCATION_DATA=true`. The current relocation and completed stage are restored after an app restart. The application has no login and is intended for one local owner.

## Deploy

Build with `docker build -t relocation-copilot .` and run with a private environment file and persistent data volume:

```bash
docker run --env-file .env -v relocation_data:/app/relocation_data -p 127.0.0.1:8501:8501 relocation-copilot
```

Put an HTTPS reverse proxy and access controls in front of the app before exposing it beyond localhost. The app itself has no login. Back up `relocation_data`, which contains run checkpoints, reports, and Mem0 data. This is designed for one owner; multi-user deployment needs account-level authentication, authorization, isolation, and operational hardening.

## Evidence and safety model

The app does not use model-generated URLs as authority. It discovers current sources through You.com Search and records the public HTTPS URL, retrieval time, and source ID. Housing leads with unknown details are labelled as needing a check; when none meet every known limit, the app shows the closest sourced options and their trade-offs. Current legal, financial, travel, and housing facts must be checked against authoritative sources. The verifier surfaces missing inputs and invalid citations. Source text is treated as untrusted data. External actions are outside this version. Do not upload passports or sensitive documents.

After housing research, the workflow carries the selected lead into budget, family commute, and shipping handoffs even when those areas have no external source yet. Their tasks remain marked as needing verification, and the page explains the next decision. Choosing **Plan around this home** replans those handoffs around that lead. Travel remains dependent on the immigration review. Sources and preferences stay with their own move, and changing the destination clears sources from the previous location.

The source fetcher blocks direct private/reserved IP addresses and redirects, caps response size, and times out. For deployment in a sensitive network, enforce outbound network policy at the proxy or container level as well; DNS can change between validation and connection.

## Validate

Run `python -m pytest -q` for 25 deterministic checks covering workflow state, constraints, failure recovery, stage selections, persistence, reset, summary output, and URL validation. Run `python -m evals.run_full_evals` for the ten-case golden dataset across all six specialists, housing constraints, citations, human review, errors, and latency. The latest reviewer-facing results are in `evals/EVALUATION_REPORT.md`; machine-readable metrics are in `evals/latest_metrics.json`. Eval evidence is synthetic and measures defined coordination behavior, not real relocation accuracy.

The annotated [Relocation_Copilot.ipynb](Relocation_Copilot.ipynb) remains a learning walkthrough. The deployable customer interface is in `app.py` and `assets/theme.css`, with an original local Seoul hero image; the application logic is in `relocation/`.

