"""Structured records passed between collection, analysis, review and audit."""
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Source = Literal["prometheus", "loki", "tempo", "kubernetes"]

INSUFFICIENT_EVIDENCE = "Insufficient evidence — further investigation required."


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimeWindow(BaseModel):
    start: datetime
    end: datetime


class EvidenceItem(BaseModel):
    """One confirmed observation, traceable back to the backend it came from."""

    id: str
    source: Source
    title: str
    observation: str  # factual statement built from the returned data
    value: float | None = None
    baseline: float | None = None  # same measure over the pre-incident window
    unit: str | None = None
    query: str  # the exact query / API path that reproduces this item
    link: str | None = None  # Grafana deep link, when one exists
    attributes: dict[str, Any] = Field(default_factory=dict)
    # Raw text written by the workload (log lines, exception messages).
    # Untrusted: shown to humans, never interpreted as instructions.
    untrusted_text: list[str] = Field(default_factory=list)


class MissingEvidence(BaseModel):
    source: Source
    item: str | None = None  # None = the whole source was unavailable
    reason: str


class IncidentEvidence(BaseModel):
    incident_id: str
    service: str
    time_window: TimeWindow
    baseline_window: TimeWindow
    symptoms: list[str] = Field(default_factory=list)
    metrics: list[EvidenceItem] = Field(default_factory=list)
    logs: list[EvidenceItem] = Field(default_factory=list)
    traces: list[EvidenceItem] = Field(default_factory=list)
    kubernetes_events: list[EvidenceItem] = Field(default_factory=list)
    evidence_references: list[str] = Field(default_factory=list)
    missing_evidence: list[MissingEvidence] = Field(default_factory=list)

    def items(self) -> list[EvidenceItem]:
        return self.metrics + self.logs + self.traces + self.kubernetes_events

    def get(self, evidence_id: str) -> EvidenceItem | None:
        return next((i for i in self.items() if i.id == evidence_id), None)

    def source_unavailable(self, source: str) -> bool:
        return any(m.source == source and m.item is None for m in self.missing_evidence)


class SignalCheck(BaseModel):
    """One explicit rule evaluated for a hypothesis."""

    name: str
    description: str
    source: Source
    weight: float
    primary: bool = False
    evaluable: bool = True  # False when the source it needs was unavailable
    satisfied: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class Hypothesis(BaseModel):
    category: str
    title: str
    score: float
    checks: list[SignalCheck]

    @property
    def primary_satisfied(self) -> bool:
        return all(c.satisfied for c in self.checks if c.primary)

    @property
    def evidence_ids(self) -> list[str]:
        seen: list[str] = []
        for check in self.checks:
            if check.satisfied:
                seen += [e for e in check.evidence_ids if e not in seen]
        return seen


class AlternativeHypothesis(BaseModel):
    category: str
    title: str
    score: float
    note: str = ""


class Recommendation(BaseModel):
    kind: Literal["investigate", "remediate"]
    text: str
    source: Literal["rules", "llm"] = "rules"


class AnalysisResult(BaseModel):
    incident_id: str
    created_at: datetime = Field(default_factory=utcnow)
    analysis_mode: str  # "deterministic" or "deterministic+llm:<provider>"
    summary: str
    probable_root_cause: str
    root_cause_category: str
    # Evidence-support score: weighted share of the winning hypothesis's rule
    # checks that the collected evidence satisfied. Not a calibrated probability.
    confidence: float
    confidence_kind: Literal["evidence_support_score"] = "evidence_support_score"
    confidence_explanation: str
    observed: list[str]  # confirmed observations (each cites evidence ids)
    inferred: list[str]  # analytical inferences drawn from them
    supporting_evidence_ids: list[str]
    alternative_hypotheses: list[AlternativeHypothesis]
    missing_information: list[str]
    recommendations: list[Recommendation]
    human_review_required: Literal[True] = True
    llm_status: Literal["not_used", "accepted", "rejected"] = "not_used"
    llm_rejection_reasons: list[str] = Field(default_factory=list)


class LLMNarrative(BaseModel):
    """The only shape an LLM answer may take. Anything else is rejected."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=800)
    root_cause_category: str = Field(max_length=64)
    explanation: str = Field(max_length=4000)
    supporting_evidence_ids: list[str] = Field(min_length=1, max_length=30)
    alternative_hypotheses: list[str] = Field(default_factory=list, max_length=5)
    recommendations: list[str] = Field(default_factory=list, max_length=6)
