"""Human review state transitions, audit records, and the no-remediation boundary."""
import ast
import sqlite3
from datetime import timedelta
from pathlib import Path

import pytest

from intelligence.analysis_engine import AnalysisEngine
from intelligence.audit import AuditStore, ReviewError
from intelligence.config import Settings
from intelligence.schemas import utcnow

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    return AuditStore(str(tmp_path / "audit.db"))


@pytest.fixture
def analysed(store, latency_evidence):
    """An incident with a stored analysis and pending recommendations."""
    now = utcnow()
    incident_id = store.create_incident("demo-api", now - timedelta(seconds=90), now, origin="scenario", scenario="latency")
    latency_evidence.incident_id = incident_id
    result = AnalysisEngine(Settings(), collector=object(), llm=None).analyze(latency_evidence)
    store.save_analysis(latency_evidence, result)
    return incident_id


def events(store, incident_id):
    return [e["event"] for e in store.audit_trail(incident_id)]


def test_incident_ids_are_sequential(store):
    now = utcnow()
    assert [store.create_incident("demo-api", now, now) for _ in range(3)] == ["INC-0001", "INC-0002", "INC-0003"]


def test_analysis_creates_pending_recommendations_and_audit_record(store, analysed):
    recommendations = store.list_recommendations(analysed)
    assert len(recommendations) == 3 and {r["status"] for r in recommendations} == {"pending"}
    assert store.get_incident(analysed)["review_status"] == "awaiting_review"
    record = next(e for e in store.audit_trail(analysed) if e["event"] == "analysis_completed")
    details = record["details"]
    assert details["mode"] == "deterministic" and details["root_cause_category"] == "latency_degradation"
    assert "M-latency_p95" in details["evidence_references"] and details["supporting_evidence_ids"]
    assert len(details["recommendations"]) == 3 and record["ts"]


def test_stored_analysis_round_trips_with_its_evidence(store, analysed, latency_evidence):
    _, evidence, result = store.latest_analysis(analysed)
    assert evidence.evidence_references == latency_evidence.evidence_references
    assert result.incident_id == analysed and result.human_review_required is True


def test_approve_records_decision_reviewer_note_and_timestamp(store, analysed):
    rec = store.list_recommendations(analysed)[0]
    decided = store.decide(rec["id"], "approve", "oren", "matches what I see in Tempo")
    assert (decided["status"], decided["reviewer"], decided["note"]) == ("approved", "oren", "matches what I see in Tempo")
    assert decided["decided_at"]
    entry = store.audit_trail(analysed)[-1]
    assert entry["event"] == "recommendation_approved" and entry["actor"] == "oren"
    assert entry["details"]["executed"] is False and entry["details"]["recommendation"] == rec["text"]


def test_rejected_recommendation_stays_recorded(store, analysed):
    rec = store.list_recommendations(analysed)[1]
    store.decide(rec["id"], "reject", "oren", "not relevant here")
    stored = next(r for r in store.list_recommendations(analysed) if r["id"] == rec["id"])
    assert stored["status"] == "rejected" and stored["note"] == "not relevant here"
    assert "recommendation_rejected" in events(store, analysed)


def test_final_status_becomes_reviewed_only_when_all_are_decided(store, analysed):
    recs = store.list_recommendations(analysed)
    for rec in recs[:-1]:
        store.decide(rec["id"], "approve", "oren")
        assert store.get_incident(analysed)["review_status"] == "awaiting_review"
    store.decide(recs[-1]["id"], "reject", "oren")
    assert store.get_incident(analysed)["review_status"] == "reviewed"
    assert store.audit_trail(analysed)[-1]["details"]["final_status"] == "reviewed"


@pytest.mark.parametrize("first,second", [("approve", "reject"), ("reject", "approve"), ("approve", "approve")])
def test_decisions_are_final(store, analysed, first, second):
    rec = store.list_recommendations(analysed)[0]
    store.decide(rec["id"], first, "oren")
    with pytest.raises(ReviewError, match="decisions are final"):
        store.decide(rec["id"], second, "someone-else")


@pytest.mark.parametrize("decision,reviewer,match", [
    ("execute", "oren", "decision must be one of"), ("approve", "  ", "reviewer name is required")])
def test_invalid_review_input_is_refused(store, analysed, decision, reviewer, match):
    rec = store.list_recommendations(analysed)[0]
    with pytest.raises(ReviewError, match=match):
        store.decide(rec["id"], decision, reviewer)
    assert store.list_recommendations(analysed)[0]["status"] == "pending"


def test_unknown_recommendation_is_refused(store):
    with pytest.raises(ReviewError, match="does not exist"):
        store.decide(999, "approve", "oren")


def test_audit_log_is_append_only(store, analysed):
    db = sqlite3.connect(store.path)
    for statement in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"):
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute(statement)
    db.close()
    assert events(store, analysed) == ["incident_recorded", "analysis_completed"]


def test_reanalysis_keeps_decided_items_as_history(store, analysed, latency_evidence):
    first = store.list_recommendations(analysed)[0]
    store.decide(first["id"], "reject", "oren", "no")
    result = AnalysisEngine(Settings(), collector=object(), llm=None).analyze(latency_evidence)
    store.save_analysis(latency_evidence, result)
    statuses = [r["status"] for r in store.list_recommendations(analysed)]
    assert statuses.count("rejected") == 1 and statuses.count("pending") == 3


def test_audit_survives_reopening_the_database(store, analysed):
    store.decide(store.list_recommendations(analysed)[0]["id"], "approve", "oren")
    reopened = AuditStore(store.path)
    assert events(reopened, analysed) == ["incident_recorded", "analysis_completed", "recommendation_approved"]


# ------------------------------------------------------ no remediation possible
FORBIDDEN_IMPORTS = {"subprocess", "os", "shutil", "socket", "paramiko", "kubernetes", "docker"}


@pytest.mark.parametrize("path", sorted((ROOT / "intelligence").glob("*.py")), ids=lambda p: p.name)
def test_intelligence_package_cannot_run_commands(path):
    """Nothing in the analysis/review code can spawn a process or a shell."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and getattr(node.func, "id", "") in ("eval", "exec", "__import__"):
            pytest.fail(f"{path.name} calls {node.func.id}()")
    allowed_os = {"config.py"}  # reads environment variables only
    offending = (imported & FORBIDDEN_IMPORTS) - ({"os"} if path.name in allowed_os else set())
    assert not offending, f"{path.name} imports {offending}"


def test_kubernetes_access_is_get_only():
    """The evidence collector's Kubernetes client is only ever used for GET."""
    source = (ROOT / "intelligence" / "evidence_collector.py").read_text(encoding="utf-8")
    for verb in (".post(", ".put(", ".patch(", ".delete("):
        assert verb not in source
    rbac = (ROOT / "kubernetes" / "evidence-reader-rbac.yaml").read_text(encoding="utf-8")
    assert 'verbs: ["get", "list"]' in rbac
    for verb in ("create", "update", "patch", "delete", "*"):
        assert f'"{verb}"' not in rbac


def test_only_the_scenario_harness_can_change_the_workload():
    """Fault-injection endpoints are called from scenarios.py and nowhere else."""
    for path in (ROOT / "intelligence").glob("*.py"):
        if path.name != "scenarios.py":
            assert "/incident/" not in path.read_text(encoding="utf-8"), path.name


def test_deciding_a_recommendation_makes_no_network_or_process_call(store, analysed, monkeypatch):
    import socket
    def boom(*args, **kwargs):
        raise AssertionError("review decision attempted a network connection")
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket.socket, "connect", boom)
    for rec, decision in zip(store.list_recommendations(analysed), ("approve", "reject", "approve")):
        store.decide(rec["id"], decision, "oren")
    assert store.get_incident(analysed)["review_status"] == "reviewed"
