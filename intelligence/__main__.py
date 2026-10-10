"""Command-line analysis of a recent time window, printed as JSON.

    python -m intelligence --minutes 5

Read-only: it queries the backends and prints the result; nothing is stored.
"""
import argparse
import json
from datetime import timedelta

from .analysis_engine import AnalysisEngine
from .config import load_settings
from .schemas import utcnow


def main() -> None:
    parser = argparse.ArgumentParser(description="Analyse the last N minutes of telemetry.")
    parser.add_argument("--minutes", type=float, default=5, help="window length (bounded to 60)")
    args = parser.parse_args()
    end = utcnow()
    evidence, result = AnalysisEngine(load_settings()).investigate("ADHOC", end - timedelta(minutes=args.minutes), end)
    print(json.dumps({
        "root_cause_category": result.root_cause_category,
        "probable_root_cause": result.probable_root_cause,
        "evidence_support_score": result.confidence,
        "symptoms": evidence.symptoms,
        "evidence_references": evidence.evidence_references,
        "supporting_evidence_ids": result.supporting_evidence_ids,
        "missing_evidence": [m.model_dump() for m in evidence.missing_evidence],
        "missing_information": result.missing_information,
        "llm_status": result.llm_status,
    }, indent=2))


if __name__ == "__main__":
    main()
