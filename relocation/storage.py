"""Small SQLite index for run discovery; LangGraph owns the checkpoint state."""

import sqlite3
import json
from datetime import datetime, timezone
from pathlib import Path


class RunIndex:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS runs (thread_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, created_at TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL, move_date TEXT NOT NULL)")
            conn.execute("CREATE TABLE IF NOT EXISTS snapshots (thread_id TEXT PRIMARY KEY, report_json TEXT NOT NULL, approval_json TEXT NOT NULL)")

    def _connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def add(self, thread_id: str, user_id: str, profile: dict, report: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT INTO runs (thread_id, user_id, created_at, origin, destination, move_date) VALUES (?, ?, ?, ?, ?, ?)",
                         (thread_id, user_id, datetime.now(timezone.utc).isoformat(),
                          profile["origin"], profile["destination"], profile["move_date"]))
            if report is not None:
                conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                             (thread_id, json.dumps(report, ensure_ascii=False), "{}"))

    def save_snapshot(self, thread_id: str, report: dict, approval: dict | None = None) -> None:
        with self._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO snapshots VALUES (?, ?, ?)",
                         (thread_id, json.dumps(report, ensure_ascii=False),
                          json.dumps(approval or {}, ensure_ascii=False)))
            profile = report.get("profile", {})
            if all(profile.get(key) for key in ("origin", "destination", "move_date")):
                conn.execute("UPDATE runs SET origin=?, destination=?, move_date=? WHERE thread_id=?",
                             (profile["origin"], profile["destination"], profile["move_date"], thread_id))

    def get_snapshot(self, thread_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT report_json, approval_json FROM snapshots WHERE thread_id=?", (thread_id,)).fetchone()
            if row is None:
                return None
            return {"report": json.loads(row[0]), "approval": json.loads(row[1])}

    def list(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM runs WHERE user_id=? ORDER BY created_at DESC LIMIT 50", (user_id,)).fetchall()
            return [dict(row) for row in rows]

    def clear(self, user_id: str) -> None:
        """Remove every saved relocation owned by this local user."""
        with self._connect() as conn:
            thread_ids = [row[0] for row in conn.execute(
                "SELECT thread_id FROM runs WHERE user_id=?", (user_id,)).fetchall()]
            if thread_ids:
                conn.executemany("DELETE FROM snapshots WHERE thread_id=?",
                                 [(thread_id,) for thread_id in thread_ids])
            conn.execute("DELETE FROM runs WHERE user_id=?", (user_id,))


class EphemeralRunIndex:
    """Process-memory run index used while persistence is disabled."""

    def __init__(self):
        self.runs: dict[str, dict] = {}
        self.snapshots: dict[str, dict] = {}

    def add(self, thread_id: str, user_id: str, profile: dict, report: dict | None = None) -> None:
        self.runs[thread_id] = {
            "thread_id": thread_id, "user_id": user_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "origin": profile["origin"], "destination": profile["destination"],
            "move_date": profile["move_date"],
        }
        if report is not None:
            self.save_snapshot(thread_id, report, {})

    def save_snapshot(self, thread_id: str, report: dict, approval: dict | None = None) -> None:
        self.snapshots[thread_id] = {
            "report": json.loads(json.dumps(report, ensure_ascii=False)),
            "approval": json.loads(json.dumps(approval or {}, ensure_ascii=False)),
        }
        profile = report.get("profile", {})
        if thread_id in self.runs and all(profile.get(key) for key in ("origin", "destination", "move_date")):
            self.runs[thread_id].update({key: profile[key] for key in ("origin", "destination", "move_date")})

    def get_snapshot(self, thread_id: str) -> dict | None:
        snapshot = self.snapshots.get(thread_id)
        return json.loads(json.dumps(snapshot, ensure_ascii=False)) if snapshot else None

    def list(self, user_id: str) -> list[dict]:
        return sorted((dict(run) for run in self.runs.values() if run["user_id"] == user_id),
                      key=lambda run: run["created_at"], reverse=True)[:50]

    def clear(self, user_id: str) -> None:
        thread_ids = [thread_id for thread_id, run in self.runs.items() if run["user_id"] == user_id]
        for thread_id in thread_ids:
            self.runs.pop(thread_id, None)
            self.snapshots.pop(thread_id, None)
