"""SQLite persistence: incidents, analyses, human review decisions, audit log.

A review decision is a record of what a human decided. Nothing here (or
anywhere else in the project) turns an approved recommendation into an action.
The audit log is append-only, enforced by database triggers.
"""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from .schemas import AnalysisResult, IncidentEvidence, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    origin      TEXT NOT NULL,           -- 'scenario' (test harness) or 'manual'
    scenario    TEXT,                    -- harness label only; never given to the analysis
    started_at  TEXT NOT NULL,
    ended_at    TEXT NOT NULL,
    recovered_at TEXT
);
CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id   TEXT NOT NULL REFERENCES incidents(id),
    created_at    TEXT NOT NULL,
    mode          TEXT NOT NULL,
    result_json   TEXT NOT NULL,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recommendations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_id INTEGER NOT NULL REFERENCES analyses(id),
    incident_id TEXT NOT NULL REFERENCES incidents(id),
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    reviewer    TEXT,
    note        TEXT,
    decided_at  TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    incident_id TEXT,
    event       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    details     TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_log_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_log_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""

DECISIONS = {"approve": "approved", "reject": "rejected"}


class ReviewError(Exception):
    pass


class AuditStore:
    def __init__(self, path: str):
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._memory = sqlite3.connect(path, check_same_thread=False) if path == ":memory:" else None
        with self._db() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _db(self):
        db = self._memory or sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        finally:
            if not self._memory:
                db.close()

    def _log(self, db, incident_id: str | None, event: str, actor: str, details: dict) -> None:
        db.execute("INSERT INTO audit_log (ts, incident_id, event, actor, details) VALUES (?,?,?,?,?)",
                   (utcnow().isoformat(), incident_id, event, actor, json.dumps(details, default=str)))

    # ------------------------------------------------------------ incidents
    def create_incident(self, service: str, started_at: datetime, ended_at: datetime,
                        origin: str = "manual", scenario: str | None = None, actor: str = "operator") -> str:
        with self._db() as db:
            number = db.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] + 1
            incident_id = f"INC-{number:04d}"
            db.execute("INSERT INTO incidents (id, service, origin, scenario, started_at, ended_at) VALUES (?,?,?,?,?,?)",
                       (incident_id, service, origin, scenario, started_at.isoformat(), ended_at.isoformat()))
            self._log(db, incident_id, "incident_recorded", actor,
                      {"origin": origin, "scenario": scenario, "window": [started_at, ended_at]})
        return incident_id

    def mark_recovered(self, incident_id: str, details: dict) -> None:
        with self._db() as db:
            db.execute("UPDATE incidents SET recovered_at = ? WHERE id = ?", (utcnow().isoformat(), incident_id))
            self._log(db, incident_id, "recovery_verified", "system", details)

    def get_incident(self, incident_id: str) -> dict | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM incidents WHERE id = ?", (incident_id,)).fetchone()
        return self._with_status(dict(row)) if row else None

    def list_incidents(self) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM incidents ORDER BY started_at DESC").fetchall()
        return [self._with_status(dict(r)) for r in rows]

    def _with_status(self, incident: dict) -> dict:
        with self._db() as db:
            analysed = db.execute("SELECT COUNT(*) FROM analyses WHERE incident_id = ?", (incident["id"],)).fetchone()[0]
            pending = db.execute("SELECT COUNT(*) FROM recommendations WHERE incident_id = ? AND status = 'pending'",
                                 (incident["id"],)).fetchone()[0]
        incident["review_status"] = ("not_analysed" if not analysed else
                                     "awaiting_review" if pending else "reviewed")
        return incident

    # ------------------------------------------------------------- analyses
    def save_analysis(self, evidence: IncidentEvidence, result: AnalysisResult) -> int:
        """Stores the analysis with the exact evidence it was based on, and
        opens one pending review item per recommendation."""
        with self._db() as db:
            # A re-analysis supersedes undecided items; decided ones stay as history.
            db.execute("DELETE FROM recommendations WHERE incident_id = ? AND status = 'pending'", (result.incident_id,))
            analysis_id = db.execute(
                "INSERT INTO analyses (incident_id, created_at, mode, result_json, evidence_json) VALUES (?,?,?,?,?)",
                (result.incident_id, result.created_at.isoformat(), result.analysis_mode,
                 result.model_dump_json(), evidence.model_dump_json())).lastrowid
            for rec in result.recommendations:
                db.execute("INSERT INTO recommendations (analysis_id, incident_id, kind, source, text) VALUES (?,?,?,?,?)",
                           (analysis_id, result.incident_id, rec.kind, rec.source, rec.text))
            self._log(db, result.incident_id, "analysis_completed", "analysis-engine", {
                "analysis_id": analysis_id, "mode": result.analysis_mode, "llm_status": result.llm_status,
                "llm_rejection_reasons": result.llm_rejection_reasons,
                "root_cause_category": result.root_cause_category, "evidence_support_score": result.confidence,
                "evidence_references": evidence.evidence_references,
                "supporting_evidence_ids": result.supporting_evidence_ids,
                "missing_evidence": [m.model_dump() for m in evidence.missing_evidence],
                "recommendations": [r.text for r in result.recommendations],
            })
        return analysis_id

    def latest_analysis(self, incident_id: str) -> tuple[int, IncidentEvidence, AnalysisResult] | None:
        with self._db() as db:
            row = db.execute("SELECT * FROM analyses WHERE incident_id = ? ORDER BY id DESC LIMIT 1",
                             (incident_id,)).fetchone()
        if not row:
            return None
        return (row["id"], IncidentEvidence.model_validate_json(row["evidence_json"]),
                AnalysisResult.model_validate_json(row["result_json"]))

    # --------------------------------------------------------------- review
    def list_recommendations(self, incident_id: str) -> list[dict]:
        with self._db() as db:
            rows = db.execute("SELECT * FROM recommendations WHERE incident_id = ? ORDER BY id", (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    def decide(self, recommendation_id: int, decision: str, reviewer: str, note: str = "") -> dict:
        """Record a human decision. Allowed transition: pending -> approved | rejected, once."""
        if decision not in DECISIONS:
            raise ReviewError(f"decision must be one of {sorted(DECISIONS)}")
        if not reviewer.strip():
            raise ReviewError("a reviewer name is required")
        with self._db() as db:
            row = db.execute("SELECT * FROM recommendations WHERE id = ?", (recommendation_id,)).fetchone()
            if not row:
                raise ReviewError(f"recommendation {recommendation_id} does not exist")
            if row["status"] != "pending":
                raise ReviewError(f"recommendation {recommendation_id} was already {row['status']}; decisions are final")
            status, now = DECISIONS[decision], utcnow().isoformat()
            db.execute("UPDATE recommendations SET status = ?, reviewer = ?, note = ?, decided_at = ? WHERE id = ?",
                       (status, reviewer.strip(), note.strip(), now, recommendation_id))
            remaining = db.execute("SELECT COUNT(*) FROM recommendations WHERE incident_id = ? AND status = 'pending'",
                                   (row["incident_id"],)).fetchone()[0]
            self._log(db, row["incident_id"], f"recommendation_{status}", reviewer.strip(), {
                "recommendation_id": recommendation_id, "analysis_id": row["analysis_id"],
                "recommendation": row["text"], "note": note.strip(),
                "final_status": "reviewed" if remaining == 0 else "awaiting_review",
                "executed": False,  # recorded decision only; the system performs no remediation
            })
            return dict(db.execute("SELECT * FROM recommendations WHERE id = ?", (recommendation_id,)).fetchone())

    # ---------------------------------------------------------------- audit
    def audit_trail(self, incident_id: str | None = None) -> list[dict]:
        query = "SELECT * FROM audit_log" + (" WHERE incident_id = ?" if incident_id else "") + " ORDER BY id"
        with self._db() as db:
            rows = db.execute(query, (incident_id,) if incident_id else ()).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    def log_event(self, incident_id: str | None, event: str, actor: str, details: dict) -> None:
        with self._db() as db:
            self._log(db, incident_id, event, actor, details)
