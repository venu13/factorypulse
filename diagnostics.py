"""Live-path diagnostics shared by scripts/preflight.py and the app's sidebar self-test.

Runs one real call each for vision inspection, structured triage and tool-calling chat and
reports PASS/FAIL with provenance, latency and the exact error text for failures.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from copilot import chat_reply, generate_work_order
from inspector import inspect_image, make_sample_image
from llm import Provenance, llm_status
from telemetry import FaultMode, analyze_window, generate_telemetry


def _row(check: str, prov: Provenance, ok_extra: bool, detail: str, expect_live: bool, wall_ms: int) -> Dict[str, Any]:
    live_ok = prov.is_live or not expect_live
    passed = live_ok and ok_extra
    if not passed:
        detail = prov.error or (f"not a live Gemini result ({prov.reason})" if not prov.is_live else detail)
    return {
        "check": check,
        "result": "PASS" if passed else "FAIL",
        "provenance": prov.label,
        "latency_ms": prov.latency_ms or wall_ms,
        "attempts": prov.attempts,
        "detail": detail,
    }


def run_preflight(expect_live: bool = True, use_mock: Optional[bool] = None) -> List[Dict[str, Any]]:
    """Run the three live checks. With expect_live=True a mock/fallback result counts as FAIL."""
    rows: List[Dict[str, Any]] = []
    df = generate_telemetry(FaultMode.BEARING_INNER_RACE, seed=7)
    anomaly = analyze_window(df)[1].to_dict()

    t = time.time()
    report, prov = inspect_image(make_sample_image("weld"), "preflight check", use_mock=use_mock)
    rows.append(_row("Vision inspection (sample weld)", prov, bool(report.defects),
                     f"{len(report.defects)} defect(s), condition {report.overall_condition}", expect_live,
                     int((time.time() - t) * 1000)))

    t = time.time()
    wo, prov = generate_work_order("PREFLIGHT-01", anomaly, report, use_mock=use_mock)
    rows.append(_row("Structured triage (WorkOrder schema)", prov, bool(wo.remediation_steps),
                     f"criticality {wo.criticality}, RUL {wo.estimated_remaining_useful_life_hours} h, "
                     f"{len(wo.required_parts)} parts", expect_live, int((time.time() - t) * 1000)))

    t = time.time()
    ctx = {"machine_id": "PREFLIGHT-01", "anomaly": anomaly, "inspection": None, "work_order": None}
    text, prov = chat_reply(
        [], "Use the lookup_sop tool for the bearing_inner_race failure mode and tell me its SOP document number.",
        ctx, use_mock=use_mock)
    tool_ok = prov.tool_calls > 0 and "SOP-MECH-021" in text
    detail = f"{prov.tool_calls} tool call(s); reply: {text[:80]!r}"
    if prov.is_live and not tool_ok and expect_live:
        detail = f"live reply but tool calling not confirmed ({prov.tool_calls} calls; SOP number present: {'SOP-MECH-021' in text})"
    rows.append(_row("Chat tool-calling (lookup_sop)", prov, tool_ok or not expect_live, detail, expect_live,
                     int((time.time() - t) * 1000)))
    return rows


def describe_environment() -> str:
    s = llm_status()
    parts = [f"mode={s['mode']}", f"model={s['model']}", f"reason={s['reason']}"]
    if s.get("project"):
        parts += [f"project={s['project']}", f"location={s.get('location')}"]
    return ", ".join(parts)
