"""FactoryPulse - Industrial Predictive Maintenance & Visual Inspection Copilot (Streamlit UI)."""
from __future__ import annotations

import hashlib
import io
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from PIL import Image
from plotly.subplots import make_subplots

from copilot import (
    WorkOrder,
    chat_reply,
    format_rul,
    generate_work_order,
    work_order_to_json,
    work_order_to_pdf,
)
import cloud
from diagnostics import describe_environment, run_preflight
from impact import ImpactAssumptions, estimate_impact
from inspector import (
    SAMPLE_KINDS,
    SEVERITY_RANK,
    InspectionReport,
    annotate_image,
    box_to_pixels,
    inspect_image,
    list_sample_gallery,
    make_sample_image,
)
from llm import Provenance, get_model, llm_status, on_cloud_run
from telemetry import (
    CHANNEL_META,
    CHANNELS,
    LIMITS,
    FaultMode,
    analyze_window,
    generate_telemetry,
)

st.set_page_config(page_title="FactoryPulse", page_icon="🏭", layout="wide", initial_sidebar_state="expanded")

# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
AMBER, GREEN, RED, BLUE = "#FFB000", "#3DDC97", "#FF4D4F", "#4DA3FF"
STATUS_COLOR = {"HEALTHY": GREEN, "WARNING": AMBER, "CRITICAL": RED}
CRIT_COLOR = {"LOW": GREEN, "MEDIUM": AMBER, "HIGH": "#FF8A1F", "CRITICAL": RED}
CHANNEL_COLOR = {
    "vibration_rms": "#4DA3FF",
    "bearing_temp_c": "#FF8A1F",
    "motor_current_a": "#B58CFF",
    "rpm": "#3DDC97",
    "acoustic_db": "#F5D90A",
}

st.markdown(
    f"""
<style>
.block-container {{ padding-top: 1.4rem; max-width: 1500px; }}
h1, h2, h3 {{ letter-spacing: .02em; }}
.fp-brand {{ font-size: 1.9rem; font-weight: 800; color: {AMBER}; letter-spacing: .08em; margin-bottom: 0; }}
.fp-sub {{ color: #8B97A8; margin-top: -.2rem; margin-bottom: 1rem; font-size: .95rem; }}
.fp-card {{ background: #151B26; border: 1px solid #263041; border-left: 4px solid var(--c, {AMBER});
  border-radius: 8px; padding: .85rem 1rem; margin-bottom: .8rem; }}
.fp-card h4 {{ margin: 0 0 .15rem 0; font-size: 1.02rem; color: #E6EAF0; }}
.fp-muted {{ color: #8B97A8; font-size: .82rem; }}
.fp-pill {{ display: inline-block; padding: .08rem .6rem; border-radius: 999px; font-size: .72rem; font-weight: 700;
  letter-spacing: .06em; color: #0B0F17; background: var(--c, {AMBER}); }}
.fp-bar {{ height: 6px; background: #263041; border-radius: 3px; margin-top: .5rem; overflow: hidden; }}
.fp-bar > div {{ height: 100%; background: var(--c, {AMBER}); }}
[data-testid="stMetric"] {{ background: #151B26; border: 1px solid #263041; border-radius: 8px; padding: .6rem .9rem; }}
[data-testid="stMetricLabel"] {{ color: #8B97A8; }}
.stTabs [data-baseweb="tab"] {{ font-weight: 600; letter-spacing: .03em; }}
.fp-wo {{ background: #151B26; border: 1px solid #263041; border-radius: 10px; padding: 1rem 1.2rem; }}
footer {{ visibility: hidden; }}
</style>
""",
    unsafe_allow_html=True,
)

# --------------------------------------------------------------------------- #
# Fleet definition & session state
# --------------------------------------------------------------------------- #
FLEET: List[Dict[str, Any]] = [
    {"id": "PUMP-101", "name": "Feedwater Pump A", "area": "Boiler House", "mode": FaultMode.NORMAL, "seed": 11},
    {"id": "PUMP-102", "name": "Cooling Circulation Pump", "area": "Utilities", "mode": FaultMode.CAVITATION, "seed": 12},
    {"id": "MTR-201", "name": "Conveyor Drive Motor", "area": "Line 2 - Packaging", "mode": FaultMode.BEARING_INNER_RACE, "seed": 13},
    {"id": "MTR-202", "name": "Extruder Main Motor", "area": "Line 3 - Extrusion", "mode": FaultMode.THERMAL_OVERLOAD, "seed": 14},
    {"id": "PUMP-301", "name": "Hydraulic Press Pump", "area": "Press Shop", "mode": FaultMode.NORMAL, "seed": 15},
    {"id": "FAN-401", "name": "Dust Collector Fan", "area": "Finishing", "mode": FaultMode.NORMAL, "seed": 16},
]
FLEET_BY_ID = {m["id"]: m for m in FLEET}
MODE_OPTIONS = [m.value for m in FaultMode]

if "initialized" not in st.session_state:
    st.session_state.initialized = True
    st.session_state.t0 = pd.Timestamp.now().floor("s").isoformat()
    st.session_state.seed_bump = 0
    st.session_state.dur_min = 60
    st.session_state.inspections = {}
    st.session_state.work_orders = {}
    st.session_state.chats = {}
    st.session_state.session_id = uuid.uuid4().hex[:12]
    st.session_state.store = cloud.make_store()
    st.session_state.logged_keys = set()
    for m in FLEET:
        st.session_state[f"cfg_mode_{m['id']}"] = m["mode"].value
        st.session_state[f"cfg_sev_{m['id']}"] = 1.0


@st.cache_data(show_spinner=False, max_entries=64)
def machine_data(mode: str, severity: float, seed: int, n_samples: int, t0: str):
    df = generate_telemetry(mode, n_samples=n_samples, severity=severity, seed=seed, end_time=pd.Timestamp(t0))
    det, report = analyze_window(df)
    return df, det, report.to_dict()


def current_data(machine_id: str):
    m = FLEET_BY_ID[machine_id]
    return machine_data(
        st.session_state[f"cfg_mode_{machine_id}"],
        float(st.session_state[f"cfg_sev_{machine_id}"]),
        m["seed"] + st.session_state.seed_bump,
        int(st.session_state.dur_min) * 12,
        st.session_state.t0,
    )


def fleet_reports() -> Dict[str, Dict[str, Any]]:
    return {m["id"]: current_data(m["id"])[2] for m in FLEET}


def pill(text: str, color: str) -> str:
    return f'<span class="fp-pill" style="--c:{color}">{text}</span>'


def fmt_mode(value: str) -> str:
    return FaultMode(value).label


def prov_badge(prov: Dict[str, Any]) -> str:
    """HTML pill showing where a result came from (live Gemini, fallback, or mock)."""
    color = {"gemini-live": GREEN, "fallback": RED, "mock": AMBER}[prov["kind"]]
    icon = {"gemini-live": "●", "fallback": "▲", "mock": "○"}[prov["kind"]]
    extra = f' · {prov["latency_ms"]} ms' if prov.get("latency_ms") else ""
    return pill(f'{icon} {prov["label"]}{extra}', color)


def prov_details(prov: Dict[str, Any]) -> None:
    """Show the real exception for fallbacks and any safety-floor overrides."""
    if prov["kind"] == "fallback":
        st.warning(f'Gemini call failed - showing the offline fallback result instead ({prov["reason"]}).')
        with st.expander("Error details (exact exception)"):
            st.code(prov["error"] or "(no message)", language="text")
            st.caption(f'attempts: {prov["attempts"]} · model: {prov["model"]}')
    elif prov["kind"] == "mock":
        st.caption(f'Offline mock result - {prov["reason"]}.')


def show_store_warnings() -> None:
    """Surface (never raise) any GCS / BigQuery problems collected so far."""
    for w in st.session_state.store.pop_warnings() + st.session_state.pop("gcs_warnings", []):
        st.warning(f"☁️ {w} - continuing without it.")


def gcs_save(blob: str, data: bytes, content_type: str) -> Optional[str]:
    uri, warn = cloud.save_to_gcs(blob, data, content_type)
    if warn:
        st.session_state.setdefault("gcs_warnings", []).append(warn)
    return uri


def log_fleet_once() -> None:
    """Log each distinct (machine, fault, severity, seed, window) telemetry state once per session."""
    store, seen = st.session_state.store, st.session_state.logged_keys
    snaps: List[dict] = []
    flags: List[dict] = []
    for m in FLEET:
        mid = m["id"]
        key = (mid, st.session_state[f"cfg_mode_{mid}"], float(st.session_state[f"cfg_sev_{mid}"]),
               st.session_state.seed_bump, st.session_state.dur_min)
        if key in seen:
            continue
        seen.add(key)
        df, _, rep = current_data(mid)
        onset = df.loc[df["fault_label"], "timestamp"].min() if df["fault_label"].any() else None
        sr, fr = cloud.build_snapshot_rows(st.session_state.session_id, mid, rep, key[1], key[2], onset)
        snaps += sr
        flags += fr
    if snaps:
        store.insert("telemetry_snapshots", snaps)
        store.insert("anomaly_flags", flags)


@st.cache_data(ttl=60, show_spinner=False)
def bigquery_analytics(dataset: str, _store) -> Dict[str, pd.DataFrame]:
    return _store.analytics()


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
status_info = llm_status()
llm_mode = status_info["mode"]
with st.sidebar:
    st.markdown('<div class="fp-brand">FACTORYPULSE</div>', unsafe_allow_html=True)
    st.markdown('<div class="fp-sub">Predictive maintenance &amp; visual inspection copilot</div>', unsafe_allow_html=True)
    force_mock = st.toggle(
        "Force offline demo mode",
        value=(llm_mode == "mock"),
        disabled=(llm_mode == "mock"),
        help="Use the built-in rule-based engine instead of calling Gemini. Off by default.",
    )
    use_mock = force_mock or llm_mode == "mock"
    if use_mock:
        st.markdown(pill("OFFLINE MOCK", AMBER), unsafe_allow_html=True)
        st.caption(f"Reason: {status_info['reason']}. Set `GEMINI_API_KEY` or Vertex AI env vars for live Gemini.")
        if on_cloud_run():
            st.error("Running on Cloud Run without live Gemini - check the service account and GOOGLE_CLOUD_PROJECT.")
    else:
        label = "GEMINI API" if llm_mode == "gemini-api" else "VERTEX AI"
        st.markdown(pill(label, GREEN), unsafe_allow_html=True)
        loc = f" · {status_info.get('project')} / {status_info.get('location')}" if llm_mode == "vertex" else ""
        st.caption(f"Model: `{get_model()}`{loc}")
    with st.expander("🩺 Diagnostics"):
        st.caption("Runs one real call each: vision, structured triage, tool-calling chat.")
        if st.button("Run Gemini self-test", use_container_width=True):
            with st.spinner("Calling Gemini..."):
                st.session_state.preflight = run_preflight(expect_live=not use_mock, use_mock=use_mock)
        if st.session_state.get("preflight"):
            st.dataframe(pd.DataFrame(st.session_state.preflight)[["check", "result", "provenance", "latency_ms", "detail"]],
                         hide_index=True, use_container_width=True)
    st.caption(f"Storage: {'gs://' + cloud.gcs_bucket() if cloud.gcs_bucket() else 'Cloud Storage off'} · "
               f"{st.session_state.store.label}")
    st.divider()
    st.caption("Simulated plant: 6 rotating assets, 5 s sampling. Inject faults in the **Telemetry Explorer** tab.")
    if st.button("🎲 Re-roll sensor noise", use_container_width=True):
        st.session_state.seed_bump += 1
        st.rerun()

st.markdown('<div class="fp-brand">🏭 FactoryPulse</div>', unsafe_allow_html=True)
st.markdown(
    '<div class="fp-sub">Industrial Predictive Maintenance &amp; Visual Inspection Copilot · Powered by Gemini on Vertex AI</div>',
    unsafe_allow_html=True,
)

ROOT_DIR = Path(__file__).resolve().parent
GALLERY_DIR = ROOT_DIR / "sample_images"
VALIDATION_PATH = ROOT_DIR / "reports" / "validation_cwru.json"
DEMO_MACHINE = "PUMP-101"


def prepare_image(data: bytes, max_side: int = 2048) -> bytes:
    """Downscale very large uploads to control token cost; leave small images untouched."""
    img = Image.open(io.BytesIO(data))
    if max(img.size) <= max_side:
        return data
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=92)
    return buf.getvalue()


def sig_for(machine_id: str) -> str:
    insp = st.session_state.inspections.get(machine_id)
    raw = f"{st.session_state[f'cfg_mode_{machine_id}']}|{st.session_state[f'cfg_sev_{machine_id}']}|{st.session_state.seed_bump}|{st.session_state.dur_min}|"
    raw += hashlib.md5(str(insp["report"]).encode()).hexdigest() if insp else "none"
    return raw


def sync_fault_cfg(machine_id: str) -> None:
    """Copy widget values into plain state keys (widget keys vanish when their widget is not rendered)."""
    st.session_state[f"cfg_mode_{machine_id}"] = st.session_state[f"w_mode_{machine_id}"]
    st.session_state[f"cfg_sev_{machine_id}"] = st.session_state[f"w_sev_{machine_id}"]


def run_inspection(machine_id: str, image_bytes: bytes, hint: str = "") -> Dict[str, Any]:
    """Inspect an image, annotate it, save to GCS (if enabled) and attach it to the machine."""
    report_obj, prov = inspect_image(image_bytes, hint, use_mock=use_mock)
    annotated = annotate_image(image_bytes, report_obj)
    buf = io.BytesIO()
    annotated.save(buf, format="PNG")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    gcs_paths = {
        "original": gcs_save(f"inspections/{machine_id}/{stamp}_original.png", image_bytes, "image/png"),
        "annotated": gcs_save(f"inspections/{machine_id}/{stamp}_annotated.png", buf.getvalue(), "image/png"),
    }
    rec = {
        "report": report_obj.model_dump(mode="json"), "prov": prov.to_dict(), "original": image_bytes,
        "annotated": buf.getvalue(), "gcs": gcs_paths, "at": datetime.now().strftime("%H:%M:%S"),
    }
    st.session_state.inspections[machine_id] = rec
    st.session_state.work_orders.pop(machine_id, None)
    return rec


def run_triage(machine_id: str, include_inspection: bool = True) -> Dict[str, Any]:
    """Generate the work order, build PDF/JSON once, save to GCS and log to the store."""
    _, _, anomaly = current_data(machine_id)
    insp_rec = st.session_state.inspections.get(machine_id)
    inspection_obj = InspectionReport.model_validate(insp_rec["report"]) if (insp_rec and include_inspection) else None
    wo, prov = generate_work_order(machine_id, anomaly, inspection_obj, use_mock=use_mock)
    meta = {"generated_at": datetime.now().strftime("%Y-%m-%d %H:%M"), "provenance": prov.label,
            "provenance_detail": prov.to_dict(), "safety_notes": prov.notes,
            "telemetry_status": anomaly["status"], "inspection_attached": inspection_obj is not None}
    pdf_bytes, json_text = work_order_to_pdf(wo, meta), work_order_to_json(wo, meta)
    pdf_uri = gcs_save(f"work_orders/{wo.incident_id}.pdf", pdf_bytes, "application/pdf")
    json_uri = gcs_save(f"work_orders/{wo.incident_id}.json", json_text.encode(), "application/json")
    st.session_state.store.insert("work_orders", [
        cloud.build_work_order_row(st.session_state.session_id, wo, prov.to_dict(), pdf_uri, json_uri)])
    rec = {"wo": wo, "prov": prov.to_dict(), "sig": sig_for(machine_id), "meta": meta,
           "pdf": pdf_bytes, "json": json_text, "gcs": {"pdf": pdf_uri, "json": json_uri}}
    st.session_state.work_orders[machine_id] = rec
    return rec


def run_demo_scenario() -> None:
    """Golden path for the demo video: fault -> anomaly -> inspection -> work order -> PDF export."""
    log: List[str] = []
    with st.status("Running demo scenario...", expanded=True) as status:
        bar = st.progress(0, text="Step 1/5 - injecting inner-race bearing fault")
        mid = DEMO_MACHINE
        st.session_state[f"cfg_mode_{mid}"] = FaultMode.BEARING_INNER_RACE.value
        st.session_state[f"cfg_sev_{mid}"] = 1.2
        for wk in (f"w_mode_{mid}", f"w_sev_{mid}"):
            st.session_state.pop(wk, None)  # let the widgets re-initialise from cfg_*
        for k in ("explorer_machine", "qa_machine", "cp_machine"):
            st.session_state[k] = mid
        st.session_state["qa_source"] = "bearing"
        log.append(f"1. Injected **inner race bearing defect** (severity 1.2) on {mid}.")
        st.write("✅ " + log[-1])

        bar.progress(20, text="Step 2/5 - running anomaly detector")
        _, _, rep = current_data(mid)
        top = rep["findings"][0]["message"] if rep["findings"] else "no findings"
        log.append(f"2. Detector status **{rep['status']}**, suspected **{fmt_mode(rep['suspected_mode'])}** "
                   f"({rep['mode_confidence']:.0%}); {len(rep['findings'])} finding(s). First: {top}")
        st.write("✅ " + log[-1])

        bar.progress(40, text="Step 3/5 - visual inspection of bearing image")
        rec = run_inspection(mid, make_sample_image("bearing"), "demo scenario: bearing race close-up")
        log.append(f"3. Visual inspection: **{rec['report']['overall_condition']}**, {len(rec['report']['defects'])} defect(s) "
                   f"[{rec['prov']['label']}].")
        st.write("✅ " + log[-1])

        bar.progress(65, text="Step 4/5 - generating work order")
        wrec = run_triage(mid, True)
        wo = wrec["wo"]
        log.append(f"4. Work order **{wo.incident_id}**: criticality **{wo.criticality}**, RUL "
                   f"{wo.estimated_remaining_useful_life_hours:.1f} h, LOTO {'required' if wo.loto_procedure_required else 'not required'} "
                   f"[{wrec['prov']['label']}].")
        st.write("✅ " + log[-1])

        bar.progress(90, text="Step 5/5 - preparing PDF export")
        log.append(f"5. PDF export ready ({len(wrec['pdf']) // 1024} KB) - download below or in the Copilot tab.")
        st.write("✅ " + log[-1])
        bar.progress(100, text="Demo scenario complete")
        status.update(label="Demo scenario complete", state="complete", expanded=False)
    st.session_state.demo_log = log
    st.session_state.demo_machine = mid


tab_overview, tab_telemetry, tab_qa, tab_copilot = st.tabs(
    ["📊 System Overview", "📈 Telemetry Anomaly Explorer", "🔍 Visual QA Lab", "🛠️ Field Engineer Copilot"]
)


# --------------------------------------------------------------------------- #
# Tab 1: Overview
# --------------------------------------------------------------------------- #
with tab_overview:
    with st.container(border=True):
        d1, d2 = st.columns([1, 3])
        clicked = d1.button("▶ Run demo scenario", type="primary", use_container_width=True,
                            help="Golden path: inject fault -> detect -> inspect -> work order -> PDF")
        d2.caption(f"One click runs the 3-minute video flow on **{DEMO_MACHINE}**: inject an inner-race bearing fault, "
                   "show the anomaly flag, inspect a bearing image, generate the work order and enable PDF export.")
        if clicked:
            run_demo_scenario()
        if st.session_state.get("demo_log"):
            with st.expander("Last demo scenario result", expanded=not clicked):
                for line in st.session_state.demo_log:
                    st.markdown(line)
                wrec = st.session_state.work_orders.get(st.session_state.demo_machine)
                if wrec:
                    x1, x2, _ = st.columns([1, 1, 2])
                    x1.download_button("📄 Download PDF", wrec["pdf"], file_name=f'{wrec["wo"].incident_id}.pdf',
                                       mime="application/pdf", key="demo_pdf")
                    x2.download_button("🧾 Download JSON", wrec["json"], file_name=f'{wrec["wo"].incident_id}.json',
                                       mime="application/json", key="demo_json")
    reports = fleet_reports()
    log_fleet_once()
    show_store_warnings()
    statuses = [r["status"] for r in reports.values()]
    avg_health = sum(r["health_score"] for r in reports.values()) / len(reports)
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Assets monitored", len(FLEET))
    k2.metric("Healthy", statuses.count("HEALTHY"))
    k3.metric("Warnings", statuses.count("WARNING"))
    k4.metric("Critical", statuses.count("CRITICAL"))
    k5.metric("Fleet health index", f"{avg_health:.0f} / 100")

    left, right = st.columns([3, 2])
    with left:
        st.subheader("Fleet status")
        cols = st.columns(3)
        for i, m in enumerate(FLEET):
            r = reports[m["id"]]
            color = STATUS_COLOR[r["status"]]
            suspected = "No fault detected" if r["suspected_mode"] == "normal" else fmt_mode(r["suspected_mode"])
            insp = st.session_state.inspections.get(m["id"])
            insp_txt = f"Visual QA: {insp['report']['overall_condition']}" if insp else "Visual QA: not inspected"
            with cols[i % 3]:
                st.markdown(
                    f'<div class="fp-card" style="--c:{color}">'
                    f'<h4>{m["id"]} {pill(r["status"], color)}</h4>'
                    f'<div class="fp-muted">{m["name"]} · {m["area"]}</div>'
                    f'<div style="margin-top:.4rem;font-size:.88rem;color:#C9D1DD">{suspected}</div>'
                    f'<div class="fp-muted">{insp_txt}</div>'
                    f'<div class="fp-bar" style="--c:{color}"><div style="width:{r["health_score"]}%"></div></div>'
                    f'<div class="fp-muted" style="margin-top:.2rem">Health {r["health_score"]:.0f}/100</div>'
                    "</div>",
                    unsafe_allow_html=True,
                )
    with right:
        st.subheader("Active alerts")
        rows = []
        for mid, r in reports.items():
            for f in r["findings"]:
                rows.append(
                    {
                        "Severity": f["severity"],
                        "Machine": mid,
                        "Channel": CHANNEL_META[f["channel"]]["label"],
                        "Kind": f["kind"],
                        "Detail": f["message"],
                    }
                )
        if rows:
            alerts = pd.DataFrame(rows)
            alerts["_r"] = alerts["Severity"].map({"CRITICAL": 0, "WARNING": 1})
            alerts = alerts.sort_values(["_r", "Machine"]).drop(columns="_r")
            st.dataframe(alerts, hide_index=True, use_container_width=True, height=400)
        else:
            st.success("No active alerts across the fleet.")

    fig = go.Figure(
        go.Bar(
            x=list(reports.keys()),
            y=[r["health_score"] for r in reports.values()],
            marker_color=[STATUS_COLOR[r["status"]] for r in reports.values()],
            text=[f'{r["health_score"]:.0f}' for r in reports.values()],
            textposition="outside",
        )
    )
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#121821", height=260,
        margin=dict(l=10, r=10, t=30, b=10), title="Asset health score", yaxis=dict(range=[0, 115], gridcolor="#222B38"),
    )
    st.plotly_chart(fig, use_container_width=True)

    # ---- Fleet analytics (BigQuery, or in-memory pandas equivalent) ------------------------- #
    st.subheader("Fleet analytics")
    store = st.session_state.store
    analytics, source_label = None, store.label
    if store.is_bigquery:
        if st.button("↻ Refresh BigQuery analytics", key="bq_refresh"):
            bigquery_analytics.clear()
        try:
            analytics = bigquery_analytics(store.label, store)
        except Exception as exc:  # noqa: BLE001
            st.warning(f"☁️ BigQuery analytics failed ({type(exc).__name__}: {exc}). Showing local data instead.")
            from cloud import LocalStore
            local = LocalStore()
            local.tables = {t: [] for t in cloud.SCHEMAS}
            analytics, source_label = local.analytics(), "local mode (BigQuery unavailable)"
    else:
        analytics = store.analytics()
    st.caption(f"Data source: **{source_label}** · rows logged this and other sessions; telemetry is simulated "
               "and fault onset is the simulator's ground truth.")
    a1, a2, a3 = st.columns(3)
    apd, mttd, wbc = (analytics["anomalies_per_day"], analytics["mean_time_to_detect"],
                      analytics["work_orders_by_criticality"])
    chart_layout = dict(template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#121821", height=300,
                        margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", y=-0.2))
    with a1:
        if len(apd):
            f1 = go.Figure([go.Bar(name=mid, x=g["day"].astype(str), y=g["anomalies"]) for mid, g in apd.groupby("machine_id")])
            f1.update_layout(title="Anomaly flags per machine per day", barmode="stack", **chart_layout)
            st.plotly_chart(f1, use_container_width=True)
        else:
            st.info("No anomaly flags logged yet.")
    with a2:
        if len(mttd):
            f2 = go.Figure(go.Bar(x=mttd["machine_id"], y=mttd["mean_detect_lag_s"] / 60.0, marker_color=BLUE,
                                  text=[f"n={int(n)}" for n in mttd["n"]]))
            f2.update_layout(title="Mean time to detect (min after fault onset)", **chart_layout)
            st.plotly_chart(f2, use_container_width=True)
        else:
            st.info("No detections with a known fault onset yet.")
    with a3:
        if len(wbc):
            order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
            f3 = go.Figure([
                go.Bar(name=k, x=g["criticality"], y=g["work_orders"])
                for k, g in wbc.groupby("provenance_kind")
            ])
            f3.update_xaxes(categoryorder="array", categoryarray=order)
            f3.update_layout(title="Work orders by criticality / provenance", barmode="stack", **chart_layout)
            st.plotly_chart(f3, use_container_width=True)
        else:
            st.info("No work orders generated yet.")

    # ---- Impact panel (assumption-driven) ------------------------------------------------------ #
    st.subheader("Estimated impact (illustrative assumptions - not measured data)")
    with st.expander("Edit assumptions", expanded=False):
        i1, i2, i3 = st.columns(3)
        cost = i1.number_input("Downtime cost per hour, USD (assumption)", 0.0, 1_000_000.0, 8000.0, 500.0)
        unplanned = i1.number_input("Unplanned failure downtime, h (assumption)", 0.0, 500.0, 12.0, 1.0)
        planned = i2.number_input("Planned repair downtime, h (assumption)", 0.0, 500.0, 3.0, 1.0)
        lead = i2.number_input("Detection lead time before failure, h (assumption)", 0.0, 720.0, 36.0, 1.0)
        window = i3.number_input("Crew planning window needed, h (assumption)", 0.0, 168.0, 8.0, 1.0)
        pprev = i3.slider("Share of acted-on alerts that prevent failure (assumption)", 0.0, 1.0, 0.8, 0.05)
    impact = estimate_impact({mid: r["status"] for mid, r in reports.items()},
                             ImpactAssumptions(cost, unplanned, planned, lead, window, pprev))
    m1, m2, m3 = st.columns(3)
    m1.metric("Assets with active alerts", len(impact["rows"]))
    m2.metric("Downtime avoided (assumption-based)", f'{impact["total_hours"]:.1f} h')
    m3.metric("Cost avoided (assumption-based)", f'${impact["total_cost"]:,.0f}')
    st.caption("**Assumption, not measurement:** estimate = (unplanned downtime − planned downtime) × P(prevented) × cost/h, "
               "counted per currently-alerting asset, and only if the assumed detection lead time covers the crew planning window. "
               "No real plant data or production outcomes were used.")


# --------------------------------------------------------------------------- #
# Tab 2: Telemetry explorer
# --------------------------------------------------------------------------- #
def telemetry_figure(df: pd.DataFrame, det: pd.DataFrame, onset_ts: Optional[pd.Timestamp], height: int = 900) -> go.Figure:
    fig = make_subplots(rows=len(CHANNELS), cols=1, shared_xaxes=True, vertical_spacing=0.03)
    for i, ch in enumerate(CHANNELS, start=1):
        meta = CHANNEL_META[ch]
        fig.add_trace(
            go.Scatter(x=df["timestamp"], y=df[ch], mode="lines", name=meta["label"],
                       line=dict(color=CHANNEL_COLOR[ch], width=1.6)),
            row=i, col=1,
        )
        flagged = det[f"{ch}_flag"].values
        if flagged.any():
            fig.add_trace(
                go.Scatter(x=df["timestamp"][flagged], y=df[ch][flagged], mode="markers", name="Anomaly",
                           marker=dict(color=RED, size=6, symbol="x"), showlegend=(i == 1),
                           legendgroup="anomaly"),
                row=i, col=1,
            )
        if ch in LIMITS:
            fig.add_hline(y=LIMITS[ch]["warn"], line=dict(color=AMBER, dash="dot", width=1), opacity=0.7, row=i, col=1)
            fig.add_hline(y=LIMITS[ch]["crit"], line=dict(color=RED, dash="dot", width=1), opacity=0.7, row=i, col=1)
        fig.update_yaxes(title_text=f"{meta['label']} ({meta['unit']})", row=i, col=1, gridcolor="#222B38",
                         title_font=dict(size=11))
    if onset_ts is not None:
        fig.add_shape(type="line", xref="x", yref="paper", x0=onset_ts.isoformat(), x1=onset_ts.isoformat(),
                      y0=0, y1=1, line=dict(color="#8B97A8", width=1, dash="dash"))
        fig.add_annotation(xref="x", yref="paper", x=onset_ts.isoformat(), y=1.02, text="fault injected",
                           showarrow=False, font=dict(color="#8B97A8", size=11))
    fig.update_xaxes(gridcolor="#222B38")
    fig.update_layout(
        template="plotly_dark", paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="#121821", height=height,
        margin=dict(l=10, r=10, t=30, b=10), legend=dict(orientation="h", y=1.04, x=0), hovermode="x unified",
    )
    return fig


def render_telemetry(machine_id: str, df: pd.DataFrame, det: pd.DataFrame, report: Dict[str, Any], window: Optional[int]) -> None:
    onset = df.loc[df["fault_label"], "timestamp"].min() if df["fault_label"].any() else None
    cols = st.columns(len(CHANNELS) + 1)
    status = report["status"]
    cols[0].markdown(
        f'<div class="fp-card" style="--c:{STATUS_COLOR[status]};padding:.5rem .8rem">'
        f'<div class="fp-muted">Status</div>{pill(status, STATUS_COLOR[status])}'
        f'<div class="fp-muted" style="margin-top:.3rem">Health {report["health_score"]:.0f}/100</div></div>',
        unsafe_allow_html=True,
    )
    for c, ch in zip(cols[1:], CHANNELS):
        s = report["snapshot"][ch]
        c.metric(
            CHANNEL_META[ch]["label"], f'{s["latest"]:.1f} {s["unit"]}',
            delta=f'{s["delta_pct"]:+.1f}% vs baseline', delta_color="off",
        )
    if window:
        df, det = df.iloc[-window:], det.iloc[-window:]
    st.plotly_chart(telemetry_figure(df, det, onset if onset is not None and onset >= df["timestamp"].iloc[0] else None),
                    use_container_width=True, key=f"tel_{machine_id}_{window}")


with tab_telemetry:
    c1, c2, c3, c4 = st.columns([2, 3, 2, 1.4])
    with c1:
        machine_id = st.selectbox("Machine", [m["id"] for m in FLEET], key="explorer_machine",
                                  format_func=lambda i: f"{i} · {FLEET_BY_ID[i]['name']}")
    with c2:
        st.radio("Fault injection", MODE_OPTIONS, index=MODE_OPTIONS.index(st.session_state[f"cfg_mode_{machine_id}"]),
                 key=f"w_mode_{machine_id}", horizontal=True, format_func=fmt_mode, on_change=sync_fault_cfg, args=(machine_id,))
    with c3:
        st.slider("Fault severity", 0.3, 2.0, value=float(st.session_state[f"cfg_sev_{machine_id}"]), step=0.1,
                  key=f"w_sev_{machine_id}", on_change=sync_fault_cfg, args=(machine_id,))
    with c4:
        st.selectbox("Window (min)", [30, 60, 120], key="dur_min")

    df, det, report = current_data(machine_id)
    live = st.toggle("▶ Live stream replay", value=False, help="Replays the window sample-by-sample, re-running the detector each tick.")

    if live:
        @st.fragment(run_every=1.0)
        def live_view() -> None:
            ckey = f"cursor_{machine_id}"
            cur = st.session_state.get(ckey, 200)
            cur = cur + 6 if cur + 6 <= len(df) else 200
            st.session_state[ckey] = cur
            sub = df.iloc[:cur]
            sub_det, sub_report = analyze_window(sub)
            st.progress(cur / len(df), text=f"Streaming sample {cur}/{len(df)}")
            render_telemetry(machine_id, sub, sub_det, sub_report.to_dict(), window=240)

        live_view()
    else:
        render_telemetry(machine_id, df, det, report, window=None)

        st.subheader("Detector findings")
        if report["findings"]:
            fdf = pd.DataFrame(report["findings"])
            fdf["channel"] = fdf["channel"].map(lambda c: CHANNEL_META[c]["label"])
            fdf["first_seen"] = pd.to_datetime(fdf["first_seen"]).dt.strftime("%H:%M:%S")
            st.dataframe(fdf[["severity", "channel", "kind", "message", "z_score", "first_seen"]],
                         hide_index=True, use_container_width=True)
        else:
            st.success("All channels within baseline envelopes.")

        d1, d2, d3 = st.columns(3)
        d1.metric("Suspected failure mode",
                  "None" if report["suspected_mode"] == "normal" else fmt_mode(report["suspected_mode"]),
                  delta=f"confidence {report['mode_confidence']:.0%}", delta_color="off")
        d2.metric("Temp slope", f"{report['snapshot']['derived']['bearing_temp_slope_c_per_hr']} °C/h")
        h = report["snapshot"]["derived"]["hours_to_temp_critical"]
        d3.metric("Hours to 95 °C limit", "n/a" if h is None else f"{h:.1f} h")
        st.download_button("⬇ Download telemetry CSV", df.to_csv(index=False).encode(),
                           file_name=f"{machine_id}_telemetry.csv", mime="text/csv")

    with st.expander("🧪 Real Data Validation - CWRU bearing dataset", expanded=False):
        if VALIDATION_PATH.exists():
            val = json.loads(VALIDATION_PATH.read_text())
            ag = val["aggregate"]
            pct = lambda v: "n/a" if v is None else f"{v:.1%}"  # noqa: E731
            st.caption(f'{val["dataset"]} · generated {val["generated_at"]} · [source]({val["source_url"]})')
            v1, v2, v3, v4 = st.columns(4)
            v1.metric("Precision", pct(ag["precision"]))
            v2.metric("Recall", pct(ag["recall"]))
            v3.metric("F1", pct(ag["f1"]))
            v4.metric("False-positive rate", pct(ag["false_positive_rate"]))
            st.markdown(
                f'Detector caught the fault in **{ag["streams_with_detection"]}/{ag["streams"]}** inner-race streams. '
                f'False alarms on held-out *normal* windows: **{ag["fp_same_load"]}/{ag["heldout_same_load"]}** at the baseline\'s '
                f'own motor load, but **{ag["fp_other_load"]}/{ag["heldout_other_load"]}** when the load differs - the fixed baseline '
                "is load-sensitive.")
            sdf = pd.DataFrame(val["streams"])[["fault", "load_hp", "precision", "recall", "false_positive_rate",
                                                "fp_same_load", "fp_other_load", "detection_delay_ms"]]
            st.dataframe(sdf, hide_index=True, use_container_width=True)
            st.markdown("**Method and limitations**")
            with st.container():
                st.json(val["method"])
                for lim in val["limitations"]:
                    st.markdown(f"- {lim}")
                for n in val.get("notes", []):
                    st.markdown(f"- note: {n}")
        else:
            st.info("No validation results found. Run `python scripts/validate_real_bearing_data.py` "
                    "(downloads the public CWRU files, or use `--data-dir`) to generate `reports/validation_cwru.json`.")


# --------------------------------------------------------------------------- #
# Tab 3: Visual QA lab
# --------------------------------------------------------------------------- #
with tab_qa:
    st.caption("Gemini multimodal reasoning detects, localizes and classifies defects, returning bounding boxes in a strict schema.")
    q1, q2 = st.columns([1, 2.2])
    with q1:
        qa_machine = st.selectbox("Associate with machine", [m["id"] for m in FLEET], key="qa_machine",
                                  format_func=lambda i: f"{i} · {FLEET_BY_ID[i]['name']}")
        gallery = list_sample_gallery(GALLERY_DIR)
        source_choice = st.radio(
            "Image source", list(SAMPLE_KINDS) + ["gallery", "upload"], key="qa_source",
            format_func=lambda k: {"upload": "Upload your own image",
                                   "gallery": f"Sample gallery - real images ({len(gallery)} in sample_images/)"}.get(
                k, f"Synthetic - {SAMPLE_KINDS.get(k, k)}"))
        uploaded = st.file_uploader("Component image", type=["png", "jpg", "jpeg", "webp"]) if source_choice == "upload" else None
        gallery_pick = None
        if source_choice == "gallery":
            if gallery:
                gallery_pick = st.selectbox("Gallery image", gallery)
            else:
                st.info("`sample_images/` has no images yet - see `sample_images/README.md` for NEU / MVTec AD instructions.")
        hint = st.text_input("Operator context (optional)", placeholder="e.g. weld on pump baseplate, 3rd shift")
        image_bytes: Optional[bytes] = None
        if source_choice == "upload":
            image_bytes = prepare_image(uploaded.getvalue()) if uploaded else None
        elif source_choice == "gallery":
            image_bytes = prepare_image((GALLERY_DIR / gallery_pick).read_bytes()) if gallery_pick else None
        else:
            image_bytes = make_sample_image(source_choice)
        run = st.button("🔍 Run visual inspection", type="primary", use_container_width=True, disabled=image_bytes is None)
        if run and image_bytes:
            with st.spinner("Analyzing image with Gemini..." if not use_mock else "Analyzing image (offline mock)..."):
                run_inspection(qa_machine, image_bytes, hint)

    with q2:
        rec = st.session_state.inspections.get(qa_machine)
        if rec is None:
            st.info("Pick an image and run an inspection. Results are attached to the selected machine for the Copilot.")
            if image_bytes:
                st.image(image_bytes, caption="Preview", use_container_width=True)
        else:
            rep = rec["report"]
            cond_color = {"PASS": GREEN, "MARGINAL": AMBER, "FAIL": RED}[rep["overall_condition"]]
            m1, m2, m3 = st.columns(3)
            m1.markdown(f'<div class="fp-card" style="--c:{cond_color}"><div class="fp-muted">Condition</div>'
                        f'{pill(rep["overall_condition"], cond_color)}</div>', unsafe_allow_html=True)
            m2.metric("Quality score", f'{rep["quality_score"]}/100')
            m3.metric("Defects found", len(rep["defects"]), delta=f'at {rec["at"]}', delta_color="off")
            st.markdown("Provenance: " + prov_badge(rec["prov"]), unsafe_allow_html=True)
            prov_details(rec["prov"])
            show_store_warnings()
            if rec["gcs"].get("annotated"):
                st.caption(f'Saved to Cloud Storage: `{rec["gcs"]["original"]}` and `{rec["gcs"]["annotated"]}`')
            tab_a, tab_b = st.tabs(["Gemini annotated", "Original"])
            tab_a.image(rec["annotated"], use_container_width=True)
            tab_b.image(rec["original"], use_container_width=True)
            st.markdown(f'**{rep["component_type"].title()}** - {rep["summary"]}')
            if rep["defects"]:
                ddf = pd.DataFrame(
                    [
                        {
                            "#": i, "Defect": d["defect_type"].replace("_", " "), "Severity": d["severity"],
                            "Confidence": f'{d["confidence"]:.0%}',
                            "Box": "yes" if d.get("box_2d") else "not localized", "Evidence": d["description"],
                            "Action": d["recommended_action"],
                        }
                        for i, d in enumerate(rep["defects"], 1)
                    ]
                )
                st.dataframe(ddf, hide_index=True, use_container_width=True)


# --------------------------------------------------------------------------- #
# Tab 4: Copilot & work orders
# --------------------------------------------------------------------------- #
with tab_copilot:
    p1, p2 = st.columns([1, 1.1])
    with p1:
        cp_machine = st.selectbox("Machine", [m["id"] for m in FLEET], key="cp_machine",
                                  format_func=lambda i: f"{i} · {FLEET_BY_ID[i]['name']}")
        _, _, anomaly = current_data(cp_machine)
        insp_rec = st.session_state.inspections.get(cp_machine)
        sc = STATUS_COLOR[anomaly["status"]]
        st.markdown(
            f'<div class="fp-card" style="--c:{sc}"><h4>Telemetry {pill(anomaly["status"], sc)}</h4>'
            f'<div class="fp-muted">Suspected: {"none" if anomaly["suspected_mode"] == "normal" else fmt_mode(anomaly["suspected_mode"])} '
            f'· {len(anomaly["findings"])} finding(s) · health {anomaly["health_score"]:.0f}/100</div></div>',
            unsafe_allow_html=True,
        )
        include_insp = False
        if insp_rec:
            rep = insp_rec["report"]
            include_insp = st.checkbox(f'Include visual inspection ({rep["overall_condition"]}, {len(rep["defects"])} defect(s))', value=True)
        else:
            st.markdown('<div class="fp-card"><h4>Visual inspection</h4><div class="fp-muted">None attached - run one in the Visual QA Lab.</div></div>',
                        unsafe_allow_html=True)

        if st.button("⚡ Run AI triage & generate work order", type="primary", use_container_width=True):
            with st.spinner("Correlating telemetry, vision and SOPs..."):
                run_triage(cp_machine, include_insp)

        rec_wo = st.session_state.work_orders.get(cp_machine)
        if rec_wo:
            wo: WorkOrder = rec_wo["wo"]
            if rec_wo["sig"] != sig_for(cp_machine):
                st.warning("Telemetry or inspection changed since this work order was generated - regenerate to refresh.")
            cc = CRIT_COLOR[wo.criticality]
            st.markdown("Provenance: " + prov_badge(rec_wo["prov"]), unsafe_allow_html=True)
            prov_details(rec_wo["prov"])
            for note in rec_wo["prov"]["notes"]:
                st.info(f"🛡️ {note}")
            st.markdown(
                f'<div class="fp-wo"><div class="fp-muted">{wo.incident_id}</div>'
                f'<h3 style="margin:.1rem 0">{wo.failure_mode}</h3>'
                f'{pill(wo.criticality, cc)} &nbsp;{pill("LOTO REQUIRED", RED) if wo.loto_procedure_required else pill("NO LOTO", GREEN)}'
                f'<p style="margin-top:.7rem"><b>Remaining useful life:</b> {format_rul(wo.estimated_remaining_useful_life_hours)}</p>'
                f'<p><b>Root cause analysis</b><br>{wo.root_cause_analysis}</p></div>',
                unsafe_allow_html=True,
            )
            with st.expander("Required parts", expanded=True):
                for part in wo.required_parts or ["None"]:
                    st.markdown(f"- {part}")
            with st.expander("Remediation procedure", expanded=True):
                for i, step in enumerate(wo.remediation_steps, 1):
                    st.markdown(f"{i}. {step}")
            show_store_warnings()
            if rec_wo["gcs"]["pdf"]:
                st.caption(f'Saved to Cloud Storage: `{rec_wo["gcs"]["pdf"]}` · `{rec_wo["gcs"]["json"]}`')
            e1, e2 = st.columns(2)
            e1.download_button("📄 Export PDF", rec_wo["pdf"], file_name=f"{wo.incident_id}.pdf",
                               mime="application/pdf", use_container_width=True)
            e2.download_button("🧾 Export JSON", rec_wo["json"], file_name=f"{wo.incident_id}.json",
                               mime="application/json", use_container_width=True)

    with p2:
        st.subheader("Root-cause chat")
        history: List[Dict[str, str]] = st.session_state.chats.setdefault(cp_machine, [])
        box = st.container(height=520, border=True)
        with box:
            if not history:
                st.markdown(f"Ask me about **{cp_machine}** - root cause, parts, LOTO steps, urgency. I can look up SOPs and the parts catalogue.")
            for msg in history:
                with st.chat_message(msg["role"]):
                    st.markdown(msg["content"])
                    if msg.get("prov"):
                        st.markdown(prov_badge(msg["prov"]) + (f' · {msg["prov"]["tool_calls"]} tool call(s)' if msg["prov"]["tool_calls"] else ""),
                                    unsafe_allow_html=True)
                        if msg["prov"]["kind"] == "fallback":
                            with st.expander("Error details (exact exception)"):
                                st.code(msg["prov"]["error"] or "(no message)", language="text")

        quick = st.columns(4)
        picked: Optional[str] = None
        for col, text in zip(quick, ["Why is it failing?", "Which parts do I need?", "LOTO steps", "How urgent is this?"]):
            if col.button(text, use_container_width=True, key=f"q_{cp_machine}_{text}"):
                picked = text
        prompt = st.chat_input("Ask the copilot...", key="chat_input") or picked
        if prompt:
            ctx = {
                "machine_id": cp_machine,
                "anomaly": anomaly,
                "inspection": insp_rec["report"] if insp_rec else None,
                "work_order": rec_wo["wo"].model_dump() if rec_wo else None,
            }
            with st.spinner("Thinking..."):
                answer, prov = chat_reply([{"role": m["role"], "content": m["content"]} for m in history], prompt, ctx, use_mock=use_mock)
            history.append({"role": "user", "content": prompt})
            history.append({"role": "assistant", "content": answer, "prov": prov.to_dict()})
            st.rerun()


st.divider()
st.caption("⚠️ **Synthetic / illustrative data:** all telemetry, SOPs, spare-part numbers, plant assets and cost figures in this "
           "demo are simulated or invented. Real-data validation uses the public CWRU bearing dataset only. "
           "Not for use in real maintenance decisions.")
