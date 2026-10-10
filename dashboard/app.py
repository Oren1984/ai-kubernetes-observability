"""Incident intelligence dashboard.

Grafana is where telemetry is explored; this app covers what Grafana does not:
running the controlled incidents, the evidence-based analysis, and the human
review with its audit trail.
"""
from datetime import datetime, timedelta

import httpx
import pandas as pd
import streamlit as st

from intelligence import finops
from intelligence.analysis_engine import AnalysisEngine, investigate_and_record
from intelligence.audit import AuditStore, ReviewError
from intelligence.config import load_settings
from intelligence.scenarios import SCENARIOS, ScenarioError, ScenarioRunner
from intelligence.schemas import utcnow

st.set_page_config(page_title="Incident Intelligence", page_icon="🛰️", layout="wide")

OBSERVED, INFERRED, RECOMMENDED = ":green-badge[Observed]", ":blue-badge[Inferred]", ":orange-badge[Recommended]"
SOURCE_LABEL = {"prometheus": "Metrics · Prometheus", "loki": "Logs · Loki",
                "tempo": "Traces · Tempo", "kubernetes": "Kubernetes API"}


@st.cache_resource
def services():
    settings = load_settings()
    store = AuditStore(settings.audit_db_path)
    return settings, store, AnalysisEngine(settings), ScenarioRunner(settings, store)


settings, store, engine, runner = services()


def fmt_time(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M:%S UTC") if iso else "—"


def grafana_dashboard(uid: str, start_iso: str | None = None, end_iso: str | None = None) -> str:
    url = f"{settings.grafana_public_url}/d/{uid}"
    if start_iso and end_iso:
        pad = timedelta(minutes=2)
        start = int((datetime.fromisoformat(start_iso) - pad).timestamp() * 1000)
        end = int((datetime.fromisoformat(end_iso) + pad).timestamp() * 1000)
        url += f"?from={start}&to={end}"
    return url


def pick_incident(key: str) -> dict | None:
    incidents = store.list_incidents()
    if not incidents:
        st.info("No incidents recorded yet. Trigger a scenario under **Incident control**.")
        return None
    labels = {i["id"]: f"{i['id']} · {fmt_time(i['started_at'])} · {i['review_status'].replace('_', ' ')}" for i in incidents}
    default = st.session_state.get("incident_id", incidents[0]["id"])
    ids = list(labels)
    chosen = st.selectbox("Incident", ids, index=ids.index(default) if default in ids else 0,
                          format_func=labels.get, key=key)
    st.session_state["incident_id"] = chosen
    return store.get_incident(chosen)


# ------------------------------------------------------------------ overview
@st.cache_data(ttl=5)
def connectivity() -> list[dict]:
    targets = {
        "Prometheus": f"{settings.prometheus_url}/-/ready", "Loki": f"{settings.loki_url}/ready",
        "Tempo": f"{settings.tempo_url}/ready", "Grafana": f"{settings.grafana_url}/api/health",
        "demo-api (in Kubernetes)": f"{settings.demo_api_url}/ready",
    }
    rows = []
    for name, url in targets.items():
        try:
            ok = httpx.get(url, timeout=3).status_code == 200
            rows.append({"Component": name, "Status": "✅ reachable" if ok else "⚠️ not ready"})
        except httpx.HTTPError as exc:
            rows.append({"Component": name, "Status": f"❌ {type(exc).__name__}"})
    return rows


@st.cache_data(ttl=5)
def workload() -> tuple[list[dict], str | None]:
    try:
        return engine.collector.workload_status(), None
    except (httpx.HTTPError, OSError) as exc:
        return [], f"Kubernetes API unavailable with the read-only token: {type(exc).__name__}"


def page_overview():
    st.title("AI-Assisted Kubernetes Observability")
    st.caption("AI investigates and recommends. Humans decide and execute.")
    conn, (deployments, k8s_error) = connectivity(), workload()
    incidents = store.list_incidents()
    healthy = all(r["Status"].startswith("✅") for r in conn) and not k8s_error \
        and all(d["ready"] >= d["desired"] for d in deployments)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("System health", "Healthy" if healthy else "Degraded")
    c2.metric("Workloads ready", f"{sum(d['ready'] >= d['desired'] for d in deployments)}/{len(deployments)}" if deployments else "n/a")
    c3.metric("Incidents recorded", len(incidents))
    c4.metric("Awaiting human review", sum(i["review_status"] == "awaiting_review" for i in incidents))

    left, right = st.columns(2)
    with left:
        st.subheader("Observability backends")
        st.dataframe(pd.DataFrame(conn), hide_index=True, width="stretch")
    with right:
        st.subheader("Kubernetes workload")
        if k8s_error:
            st.warning(k8s_error)
        else:
            st.dataframe(pd.DataFrame(deployments), hide_index=True, width="stretch")
            st.caption(f"Namespace `{settings.namespace}`, read through the read-only `evidence-reader` service account.")

    st.subheader("Recent incidents")
    if incidents:
        st.dataframe(pd.DataFrame([{
            "Incident": i["id"], "Origin": i["origin"], "Started": fmt_time(i["started_at"]),
            "Ended": fmt_time(i["ended_at"]), "Recovery verified": fmt_time(i["recovered_at"]),
            "Review": i["review_status"].replace("_", " ")} for i in incidents[:10]]), hide_index=True, width="stretch")
    else:
        st.info("No incidents recorded yet.")

    st.subheader("Grafana")
    g1, g2 = st.columns(2)
    g1.link_button("Kubernetes & Service Health", grafana_dashboard("aiobs-health"), width="stretch")
    g2.link_button("Incident Investigation", grafana_dashboard("aiobs-incident"), width="stretch")


# ----------------------------------------------------------- incident control
@st.fragment(run_every=5)
def scenario_state():
    active = runner.active_incident()
    if active:
        total = (datetime.fromisoformat(active["ended_at"]) - datetime.fromisoformat(active["started_at"])).total_seconds()
        left = max(0.0, (datetime.fromisoformat(active["ended_at"]) - utcnow()).total_seconds())
        st.warning(f"**{active['id']}** is running — scenario `{active['scenario']}` · "
                   f"started {fmt_time(active['started_at'])} · window ends {fmt_time(active['ended_at'])}")
        st.progress(1 - left / total, text=f"{left:.0f}s of the incident window remaining")
    else:
        st.success("No scenario is running.")
    for key, scenario in SCENARIOS.items():
        with st.container(border=True):
            cooldown = runner.cooldown_remaining(key)
            a, b = st.columns([4, 1])
            a.markdown(f"**Scenario {scenario.title}**  \n{scenario.description}")
            a.caption(f"Incident window {scenario.duration_s}s · cooldown {scenario.cooldown_s}s"
                      + (f" · **cooling down, {cooldown}s left**" if cooldown and not active else ""))
            if b.button("Trigger", key=f"trigger-{key}", disabled=bool(active or cooldown), width="stretch"):
                try:
                    st.session_state["incident_id"] = runner.trigger(key)
                    st.rerun()
                except (ScenarioError, httpx.HTTPError) as exc:
                    st.error(f"Could not trigger: {exc}")


def page_control():
    st.title("Incident control")
    st.caption("Test harness for the three controlled incidents. One scenario runs at a time; each is bounded and "
               "has a cooldown. This page is the only place that changes workload behaviour, and only when you click.")
    scenario_state()

    st.subheader("Recovery")
    a, b = st.columns(2)
    if a.button("Reset injected faults (test harness)", width="stretch"):
        try:
            runner.reset()
            st.toast("Latency and error faults cleared.")
        except httpx.HTTPError as exc:
            st.error(f"Reset failed: {exc}")
    if b.button("Verify healthy state from telemetry", width="stretch"):
        last = next((i for i in store.list_incidents() if i["origin"] == "scenario"), None)
        finished = last and last["ended_at"] <= utcnow().isoformat()
        status = runner.check_recovery(last["id"] if finished and not last["recovered_at"] else None)
        (st.success if status["healthy"] else st.warning)(
            "Healthy: service responds and telemetry is back under thresholds." if status["healthy"]
            else "Not confirmed healthy yet.")
        st.dataframe(pd.DataFrame([{"Check": k, "Result": {True: "✅ pass", False: "❌ fail", None: "❔ no data"}[v]}
                                   for k, v in status["checks"].items()]), hide_index=True, width="stretch")

    runs = [i for i in store.list_incidents() if i["origin"] == "scenario"]
    if runs:
        st.subheader("Scenario runs")
        st.dataframe(pd.DataFrame([{
            "Incident": i["id"], "Scenario": i["scenario"], "Start": fmt_time(i["started_at"]),
            "End": fmt_time(i["ended_at"]), "Recovery verified": fmt_time(i["recovered_at"])} for i in runs[:10]]),
            hide_index=True, width="stretch")


# -------------------------------------------------------------- investigation
def page_investigation():
    st.title("AI investigation")
    with st.expander("Investigate an arbitrary time window (no scenario)"):
        minutes = st.slider("Analyse the last N minutes", 1, settings.max_window_minutes, 5)
        if st.button("Record window as incident and analyse"):
            end = utcnow()
            incident_id = store.create_incident(settings.service, end - timedelta(minutes=minutes), end)
            investigate_and_record(engine, store, store.get_incident(incident_id))
            st.session_state["incident_id"] = incident_id
            st.rerun()

    incident = pick_incident("investigation-incident")
    if not incident:
        return
    running = incident["ended_at"] > utcnow().isoformat()
    a, b, c = st.columns([2, 1, 1])
    a.caption(f"Window {fmt_time(incident['started_at'])} → {fmt_time(incident['ended_at'])}"
              + (" · **still running: analysis will cover the window so far**" if running else ""))
    if b.button("Collect evidence & analyse", type="primary", width="stretch"):
        with st.spinner("Querying Prometheus, Tempo, Loki and Kubernetes…"):
            investigate_and_record(engine, store, incident)
        st.rerun()
    c.link_button("Open window in Grafana", grafana_dashboard("aiobs-incident", incident["started_at"], incident["ended_at"]),
                  width="stretch")

    latest = store.latest_analysis(incident["id"])
    if not latest:
        st.info("This incident has not been analysed yet.")
        return
    _, evidence, result = latest

    st.subheader("Finding")
    left, right = st.columns([3, 1])
    left.markdown(f"**Probable root cause**  {INFERRED}  \n### {result.probable_root_cause}")
    left.write(result.summary)
    right.metric("Evidence-support score", f"{result.confidence:.2f}")
    right.progress(min(result.confidence, 1.0))
    right.caption("Share of the expected evidence pattern that was observed. Not a calibrated probability.")
    mode = {"not_used": "rules only", "accepted": "narrative accepted after validation",
            "rejected": "narrative REJECTED by validation — rule-based output shown"}[result.llm_status]
    st.caption(f"Analysis mode `{result.analysis_mode}` · LLM narrative: {mode} · analysed {fmt_time(result.created_at.isoformat())}")
    if result.llm_rejection_reasons:
        st.error("LLM output rejected: " + "; ".join(result.llm_rejection_reasons))
    with st.expander("How the score was calculated"):
        st.text(result.confidence_explanation)

    col_obs, col_inf = st.columns(2)
    with col_obs:
        st.markdown(f"#### {OBSERVED} Confirmed by telemetry")
        for line in result.observed or ["Nothing relevant was observed."]:
            st.markdown(f"- {line}")
        if evidence.symptoms:
            st.caption("Symptoms detected: " + ", ".join(s.replace("_", " ") for s in evidence.symptoms))
    with col_inf:
        st.markdown(f"#### {INFERRED} Analytical inference")
        for line in result.inferred or ["No inference was drawn."]:
            st.markdown(f"- {line}")
        if result.alternative_hypotheses:
            st.markdown("**Alternative explanations considered**")
            for alt in result.alternative_hypotheses:
                st.markdown(f"- {alt.title} — score {alt.score:.2f}" + (f"  \n  :gray[{alt.note}]" if alt.note else ""))

    st.markdown(f"#### {RECOMMENDED} Next steps for a human")
    for rec in result.recommendations or []:
        st.markdown(f"- **{rec.kind}** · {rec.text}" + ("  :gray[(suggested by LLM)]" if rec.source == "llm" else ""))
    st.caption("Recommendations are advisory. Decide on them under **Review & audit**; nothing is executed by this system.")

    if result.missing_information:
        st.markdown("#### Missing information")
        for line in result.missing_information:
            st.markdown(f"- ⚠️ {line}")

    st.subheader("Evidence")
    st.caption("Every item is a query result from a live backend. Items cited by the finding are marked ●.")
    for source, label in SOURCE_LABEL.items():
        items = [i for i in evidence.items() if i.source == source]
        with st.expander(f"{label} — {len(items)} items", expanded=bool(items) and source != "kubernetes"):
            if evidence.source_unavailable(source):
                st.error("Source unavailable during collection — no evidence was substituted.")
            for item in items:
                cited = "●" if item.id in result.supporting_evidence_ids else "○"
                st.markdown(f"{cited} `{item.id}` — {item.observation}" + (f"  [open in Grafana]({item.link})" if item.link else ""))
                st.caption(f"Query: `{item.query}`")
                for text in item.untrusted_text:
                    st.code(text, language=None, wrap_lines=True)


# ------------------------------------------------------------- review & audit
def page_review():
    st.title("Human review & audit")
    st.info("Approving or rejecting **records your decision**. It does not execute anything: this system has no "
            "ability to change the cluster, and no remediation is automated.")
    incident = pick_incident("review-incident")
    if not incident:
        return
    recommendations = store.list_recommendations(incident["id"])
    if not recommendations:
        st.info("No recommendations yet — run the analysis for this incident first.")
    reviewer = st.text_input("Reviewer name", value=st.session_state.get("reviewer", ""), placeholder="Your name")
    st.session_state["reviewer"] = reviewer
    for rec in recommendations:
        with st.container(border=True):
            st.markdown(f"**#{rec['id']} · {rec['kind']}** — {rec['text']}")
            if rec["status"] == "pending":
                note = st.text_input("Decision note", key=f"note-{rec['id']}", placeholder="Why approve or reject?")
                a, b, _ = st.columns([1, 1, 4])
                for column, decision in ((a, "approve"), (b, "reject")):
                    if column.button(decision.capitalize(), key=f"{decision}-{rec['id']}", width="stretch"):
                        try:
                            store.decide(rec["id"], decision, reviewer, note)
                            st.rerun()
                        except ReviewError as exc:
                            st.error(str(exc))
            else:
                icon = "✅" if rec["status"] == "approved" else "🚫"
                st.markdown(f"{icon} **{rec['status'].upper()}** by {rec['reviewer']} at {fmt_time(rec['decided_at'])}"
                            + (f" — “{rec['note']}”" if rec["note"] else ""))

    st.subheader("Audit trail")
    scope = st.radio("Scope", ["This incident", "Everything"], horizontal=True, label_visibility="collapsed")
    trail = store.audit_trail(incident["id"] if scope == "This incident" else None)
    st.dataframe(pd.DataFrame([{
        "#": e["id"], "Time (UTC)": fmt_time(e["ts"]), "Incident": e["incident_id"] or "—", "Event": e["event"],
        "Actor": e["actor"], "Details": str({k: v for k, v in e["details"].items() if k != "evidence_references"})}
        for e in reversed(trail)]), hide_index=True, width="stretch")
    st.caption("Append-only: the database rejects updates and deletes on the audit log.")


# --------------------------------------------------------------------- finops
def page_finops():
    st.title("Telemetry volume & cost awareness")
    st.caption("Local measurements, simple arithmetic, and clearly hypothetical prices. This is not AWS billing data.")
    measured = finops.measure(settings)
    st.subheader(":green-badge[Measured] Local telemetry throughput (5-minute rate)")
    show = lambda v, f: "n/a" if v is None else f(v)
    cols = st.columns(4)
    cols[0].metric("Spans / s", show(measured["spans_per_s"], lambda v: f"{v:.1f}"))
    cols[1].metric("Log records / s", show(measured["log_records_per_s"], lambda v: f"{v:.2f}"))
    cols[2].metric("Metric points / s", show(measured["metric_points_per_s"], lambda v: f"{v:.1f}"))
    cols[3].metric("Workload active series", show(measured["active_series"], lambda v: f"{v:.0f}"),
                   help="Series from the project's namespaces and services. The local TSDB holds more, "
                        "because Prometheus also scrapes the backends' own metrics.")
    cols = st.columns(4)
    cols[0].metric("Trace ingest", show(measured["trace_bytes_per_s"], lambda v: f"{v / 1024:.1f} KiB/s"))
    cols[1].metric("Log ingest", show(measured["log_bytes_per_s"], lambda v: f"{v / 1024:.2f} KiB/s"))
    cols[2].metric("Prometheus TSDB on disk", show(measured["prometheus_storage_bytes"], lambda v: f"{v / 2**20:.1f} MiB"))

    st.subheader(":green-badge[Measured] Workload requests vs usage")
    rows = finops.workload_resources(settings)
    if rows:
        mib = lambda r, k: round(r[k] / 2**20, 1) if k in r else None
        st.dataframe(pd.DataFrame([{
            "Container": r["container"],
            "CPU request (cores)": r.get("cpu_request_cores"), "CPU usage (cores)": round(r.get("cpu_usage_cores", 0), 4),
            "Memory request (MiB)": mib(r, "memory_request_bytes"), "Memory limit (MiB)": mib(r, "memory_limit_bytes"),
            "Memory working set (MiB)": mib(r, "memory_working_set_bytes")} for r in rows]), hide_index=True, width="stretch")

    st.subheader(":blue-badge[Calculated] Monthly projection · :orange-badge[Hypothetical] prices")
    a, b = st.columns(2)
    sampling = a.slider("Trace head-sampling ratio", 0.01, 1.0, 1.0, 0.01, help="1.0 = keep every trace (what this POC does)")
    traffic = b.slider("Traffic multiplier vs. this demo (2 req/s)", 1, 1000, 1)
    prices = finops.PriceAssumptions()
    if None in (measured["log_bytes_per_s"], measured["trace_bytes_per_s"], measured["active_series"]):
        st.warning("Throughput is not measurable right now, so no projection is shown.")
        return
    est = finops.estimate_monthly(measured["log_bytes_per_s"], measured["trace_bytes_per_s"],
                                  measured["active_series"], sampling, traffic, prices)
    st.dataframe(pd.DataFrame([
        {"Signal": "Traces", "Projected volume": f"{est['trace_gb_per_month']:.2f} GB / month",
         "Assumed unit price": f"${prices.trace_ingest_usd_per_gb:.2f} / GB ingested", "Estimated cost": f"${est['traces_usd']:.2f}"},
        {"Signal": "Logs", "Projected volume": f"{est['log_gb_per_month']:.2f} GB / month",
         "Assumed unit price": f"${prices.log_ingest_usd_per_gb:.2f} / GB ingested", "Estimated cost": f"${est['logs_usd']:.2f}"},
        {"Signal": "Metrics", "Projected volume": f"{est['active_series']:.0f} active series",
         "Assumed unit price": f"${prices.metric_usd_per_1k_active_series:.2f} / 1k series / month", "Estimated cost": f"${est['metrics_usd']:.2f}"},
    ]), hide_index=True, width="stretch")
    st.metric("Estimated total", f"${est['total_usd']:.2f} / month")
    st.caption("Assumptions: 30-day month; logs and traces scale linearly with traffic; trace volume scales with the "
               "sampling ratio; active series do not grow with traffic; local retention is 24h for all three backends. "
               "Unit prices are illustrative placeholders, not vendor quotes.")


PAGES = {"Overview": page_overview, "Incident control": page_control, "AI investigation": page_investigation,
         "Review & audit": page_review, "Telemetry cost": page_finops}
with st.sidebar:
    st.header("🛰️ Incident Intelligence")
    page = st.radio("Navigate", list(PAGES), label_visibility="collapsed")
    st.divider()
    st.link_button("Open Grafana", settings.grafana_public_url, width="stretch")
    st.caption(f"Service `{settings.service}` · LLM provider `{settings.llm_provider}`")
PAGES[page]()
