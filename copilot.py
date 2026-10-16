"""FactoryPulse operational copilot.

Correlates anomaly telemetry and visual inspection findings with SOPs to produce a
schema-enforced WorkOrder via Gemini structured outputs, and powers a tool-calling
root-cause chat. A deterministic rule-based engine provides an offline fallback.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fpdf import FPDF
from pydantic import BaseModel, Field, field_validator

from inspector import (
    DEFAULT_MODEL,
    SEVERITY_RANK,
    InspectionReport,
    get_genai_client,
    get_llm_mode,
)

logger = logging.getLogger("factorypulse.copilot")

CRITICALITY_LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


# --------------------------------------------------------------------------- #
# Output schema
# --------------------------------------------------------------------------- #
class WorkOrder(BaseModel):
    incident_id: str
    machine_id: str
    criticality: str = Field(description="LOW, MEDIUM, HIGH, or CRITICAL")
    failure_mode: str
    root_cause_analysis: str
    estimated_remaining_useful_life_hours: float
    required_parts: List[str]
    loto_procedure_required: bool
    remediation_steps: List[str]

    @field_validator("criticality")
    @classmethod
    def _normalize_criticality(cls, v: str) -> str:
        v = v.strip().upper()
        if v not in CRITICALITY_LEVELS:
            raise ValueError(f"criticality must be one of {CRITICALITY_LEVELS}")
        return v


# --------------------------------------------------------------------------- #
# Knowledge base: SOPs and spare-parts catalogue
# --------------------------------------------------------------------------- #
LOTO_STEPS = [
    "Notify operations and the area supervisor; obtain a permit-to-work for SOP-LOTO-001.",
    "Stop the unit using the normal stop sequence and confirm the shaft has come to rest.",
    "Open and lock out the motor breaker at the MCC; apply personal lock and danger tag.",
    "Close and lock suction and discharge isolation valves; bleed residual pressure at the vent/drain.",
    "Verify zero energy: attempt a start from the local panel, test for dead with a voltmeter, confirm 0 bar.",
]
RETURN_TO_SERVICE_STEPS = [
    "Remove tools and guards check, then remove personal locks and tags in reverse order (SOP-LOTO-001 §7).",
    "Perform a supervised restart; trend vibration, temperature and current for 30 minutes against baseline.",
    "Close the work order with as-found/as-left readings and attach photos of the removed component.",
]

SOP_LIBRARY: Dict[str, Dict[str, Any]] = {
    "bearing_inner_race": {
        "doc": "SOP-MECH-021",
        "title": "Rolling-element bearing inner race defect response",
        "failure_mode": "Inner race bearing defect (rolling-contact fatigue)",
        "summary": "Impulsive vibration with elevated peak/median ratio, rising bearing temperature and "
        "acoustic level while motor current stays stable indicates a spalled inner race. Replace both "
        "bearings within the maintenance window; do not run above 7.1 mm/s RMS.",
        "parts": ["SKF-6313-2RS1/C3", "SEAL-LIP-65x90x10", "GRS-EP2-400G"],
        "steps": [
            "Remove coupling guard and coupling spacer; record alignment readings (as-found).",
            "Pull the DE and NDE bearing housings; extract bearings with a puller, never by hammering the cage.",
            "Inspect shaft journals for fretting and runout (limit 0.02 mm); polish or replace the shaft if damaged.",
            "Fit new SKF-6313-2RS1/C3 bearings using an induction heater (max 110 °C) and replace lip seals.",
            "Pack with GRS-EP2 grease to 30 % of cavity volume; refit housings and torque to 85 N·m.",
            "Re-align the coupling to <0.05 mm offset / 0.05 mm/100 mm angular and refit the guard.",
        ],
        "typical_rul_hours": 120.0,
    },
    "thermal_overload": {
        "doc": "SOP-ELEC-007",
        "title": "Motor thermal overload response",
        "failure_mode": "Motor thermal overload (sustained over-current / cooling loss)",
        "summary": "Exponential temperature rise with rising current and falling speed (slip) indicates "
        "overload or loss of cooling. Trip the unit at 95 °C bearing temperature; winding insulation life "
        "halves for every 10 °C above rating.",
        "parts": ["PTC-THM-150C", "OLR-CNT-65A", "FAN-CVR-280", "MEG-TEST-KIT"],
        "steps": [
            "Reduce load / throttle discharge immediately if the unit cannot be stopped at once.",
            "Inspect the cooling fan, fan cover and ventilation path for blockage; clean or replace FAN-CVR-280.",
            "Megger-test winding insulation (min 100 MΩ at 500 V) and check phase current balance (<5 %).",
            "Verify and re-set the overload relay OLR-CNT-65A; replace PTC thermistors if readings are erratic.",
            "Check the driven load for jamming or process overload; confirm pump curve operating point.",
        ],
        "typical_rul_hours": 24.0,
    },
    "cavitation": {
        "doc": "SOP-HYD-014",
        "title": "Centrifugal pump cavitation response",
        "failure_mode": "Hydraulic cavitation (insufficient NPSH available)",
        "summary": "Erratic motor current and acoustic oscillation with crackling noise indicate vapour "
        "bubble collapse at the impeller. Sustained operation erodes the impeller and damages the seal.",
        "parts": ["IMP-CP250-SS316", "MSL-CP250-SIC", "GKT-VOL-250", "STR-SUC-DN100"],
        "steps": [
            "Check suction conditions: tank level, suction strainer differential pressure, valve fully open.",
            "Compare NPSHa to NPSHr at the current flow; throttle discharge to move left on the curve if needed.",
            "Inspect the impeller for pitting/erosion at the vane inlet; replace IMP-CP250-SS316 if loss > 10 %.",
            "Replace mechanical seal MSL-CP250-SIC and gasket set GKT-VOL-250 whenever the casing is opened.",
            "Clean or replace suction strainer STR-SUC-DN100 and verify suction line is free of air ingress.",
        ],
        "typical_rul_hours": 96.0,
    },
}

VISUAL_SOPS: Dict[str, Dict[str, Any]] = {
    "hairline_fatigue_crack": {
        "doc": "SOP-STR-014",
        "criticality": "CRITICAL",
        "parts": ["WLD-ROD-308L-KIT", "PT-DYE-KIT"],
        "steps": [
            "Quarantine the component and tag it DO NOT USE; do not attempt to run it.",
            "Confirm crack extent with dye-penetrant (ASTM E1417) or MPI; mark crack tips.",
            "Replace the component or repair per approved WPS (grind out, re-weld with WLD-ROD-308L, PWHT, re-NDT).",
        ],
    },
    "heat_discoloration": {
        "doc": "SOP-MAT-006",
        "criticality": "MEDIUM",
        "parts": ["HRD-TEST-BLOCK"],
        "steps": ["Hardness-test the discoloured zone; investigate lubrication and cooling at the source."],
    },
    "surface_pitting": {
        "doc": "SOP-MECH-033",
        "criticality": "HIGH",
        "parts": ["OIL-ISO-VG220-20L", "FLT-OIL-10U"],
        "steps": ["Replace lubricant and filter (OIL-ISO-VG220, FLT-OIL-10U); send an oil sample for particle count."],
    },
    "excessive_wear": {
        "doc": "SOP-MECH-035",
        "criticality": "HIGH",
        "parts": ["GEAR-SPUR-Z22-M5"],
        "steps": ["Measure tooth thickness against tolerance; replace the gear GEAR-SPUR-Z22-M5 if loss exceeds 10 %."],
    },
    "corrosion": {
        "doc": "SOP-MAT-011",
        "criticality": "MEDIUM",
        "parts": ["CTG-EPX-2K-5L"],
        "steps": ["Blast-clean to Sa 2.5, measure wall loss, and recoat with CTG-EPX-2K."],
    },
    "weld_porosity": {
        "doc": "SOP-STR-016",
        "criticality": "MEDIUM",
        "parts": ["WLD-ROD-308L-KIT"],
        "steps": ["Grind out porous metal and re-weld per WPS; radiograph the repaired seam."],
    },
    "other": {"doc": "SOP-GEN-001", "criticality": "LOW", "parts": [], "steps": ["Escalate for manual inspection."]},
}

PARTS_CATALOG: Dict[str, str] = {
    "SKF-6313-2RS1/C3": "Deep-groove ball bearing 65x140x33 mm, sealed, C3 clearance",
    "SEAL-LIP-65x90x10": "Radial shaft lip seal 65x90x10 mm NBR",
    "GRS-EP2-400G": "EP2 lithium-complex bearing grease, 400 g cartridge",
    "PTC-THM-150C": "PTC thermistor set (3x), 150 °C trip",
    "OLR-CNT-65A": "Thermal overload relay 48-65 A",
    "FAN-CVR-280": "Motor cooling fan cover, frame 280",
    "MEG-TEST-KIT": "Insulation resistance test consumables",
    "IMP-CP250-SS316": "Closed impeller 250 mm, SS316",
    "MSL-CP250-SIC": "Mechanical seal, SiC/SiC, 250-series pump",
    "GKT-VOL-250": "Volute gasket set, 250-series pump",
    "STR-SUC-DN100": "Suction strainer DN100, 1.5 mm mesh",
    "WLD-ROD-308L-KIT": "308L stainless electrode kit with WPS card",
    "PT-DYE-KIT": "Dye-penetrant NDT kit (cleaner, penetrant, developer)",
    "HRD-TEST-BLOCK": "Hardness test reference block",
    "OIL-ISO-VG220-20L": "ISO VG220 gear oil, 20 L",
    "FLT-OIL-10U": "10 micron oil filter element",
    "GEAR-SPUR-Z22-M5": "Spur gear, 22 teeth, module 5",
    "CTG-EPX-2K-5L": "Two-component epoxy protective coating, 5 L",
}


def _part_string(part_no: str, qty: int = 1) -> str:
    return f"{part_no} - {PARTS_CATALOG.get(part_no, 'Part')} (qty {qty})"


def lookup_sop(failure_mode: str) -> Dict[str, Any]:
    """Look up the standard operating procedure for a failure mode or defect type.

    Args:
        failure_mode: One of bearing_inner_race, thermal_overload, cavitation,
            hairline_fatigue_crack, heat_discoloration, surface_pitting, excessive_wear,
            corrosion, weld_porosity.

    Returns:
        A dictionary with the SOP document number, summary, required parts and steps.
    """
    key = failure_mode.strip().lower().replace(" ", "_").replace("-", "_")
    if key in SOP_LIBRARY:
        s = SOP_LIBRARY[key]
        return {"doc": s["doc"], "title": s["title"], "summary": s["summary"], "parts": s["parts"], "steps": s["steps"]}
    if key in VISUAL_SOPS:
        s = VISUAL_SOPS[key]
        return {"doc": s["doc"], "parts": s["parts"], "steps": s["steps"]}
    return {"error": f"No SOP found for '{failure_mode}'.", "available": list(SOP_LIBRARY) + list(VISUAL_SOPS)}


def lookup_spare_part(query: str) -> List[Dict[str, str]]:
    """Search the spare-parts catalogue by part number or keyword.

    Args:
        query: Part number fragment or description keyword, e.g. 'bearing' or 'SKF'.

    Returns:
        A list of matching parts with part_number and description.
    """
    q = query.strip().lower()
    return [{"part_number": pn, "description": d} for pn, d in PARTS_CATALOG.items() if q in pn.lower() or q in d.lower()]


def retrieve_context(anomaly: Dict[str, Any], inspection: Optional[InspectionReport]) -> List[Dict[str, Any]]:
    """Select SOP documents relevant to the detected failure mode and visual defects."""
    docs: List[Dict[str, Any]] = []
    mode = anomaly.get("suspected_mode", "normal")
    if mode in SOP_LIBRARY:
        docs.append({"key": mode, **lookup_sop(mode)})
    if inspection:
        for key in sorted({d.defect_type.value for d in inspection.defects}):
            docs.append({"key": key, **lookup_sop(key)})
    return docs


# --------------------------------------------------------------------------- #
# Work-order generation
# --------------------------------------------------------------------------- #
TRIAGE_SYSTEM = """You are FactoryPulse Copilot, a senior reliability engineer assisting field maintenance
technicians at a manufacturing plant. You correlate condition-monitoring telemetry, visual inspection findings
and company SOPs to produce a precise, safe, actionable incident work order.

Rules:
- Ground every claim in the supplied telemetry numbers, inspection findings or SOP text; cite SOP document numbers.
- criticality must be exactly LOW, MEDIUM, HIGH or CRITICAL. Use CRITICAL when failure is likely within a shift
  or a safety-relevant crack exists; HIGH if action is needed within 72 h.
- estimated_remaining_useful_life_hours must be a defensible number given the trend (use hours_to_temp_critical
  when provided); never return a negative number.
- required_parts must use catalogue part numbers from the SOPs, formatted "PART-NO - description (qty N)".
- loto_procedure_required must be true for any intervention on rotating or energised equipment.
- remediation_steps must be ordered, imperative, and begin with the lock-out/tag-out sequence when LOTO is required,
  and end with return-to-service verification.
- If the telemetry is healthy and there are no defects, say so and set criticality LOW."""


def _incident_id(machine_id: str, anomaly: Dict[str, Any]) -> str:
    stamp = anomaly.get("window_end") or datetime.now().isoformat()
    try:
        stamp_s = datetime.fromisoformat(stamp).strftime("%Y%m%d-%H%M")
    except ValueError:
        stamp_s = datetime.now().strftime("%Y%m%d-%H%M")
    return f"INC-{machine_id}-{stamp_s}"


def _apply_guardrails(wo: WorkOrder, machine_id: str, anomaly: Dict[str, Any], inspection: Optional[InspectionReport]) -> WorkOrder:
    """Deterministic safety floors so a model error can never under-rate an incident."""
    floor = 0
    status = anomaly.get("status", "HEALTHY")
    floor = max(floor, {"CRITICAL": 2, "WARNING": 1}.get(status, 0))
    if inspection:
        for d in inspection.defects:
            floor = max(floor, SEVERITY_RANK[d.severity] - 1)
    data = wo.model_dump()
    if CRITICALITY_LEVELS.index(data["criticality"]) < floor:
        data["criticality"] = CRITICALITY_LEVELS[floor]
    if not data["incident_id"].strip():
        data["incident_id"] = _incident_id(machine_id, anomaly)
    data["machine_id"] = machine_id
    data["estimated_remaining_useful_life_hours"] = max(0.0, round(float(data["estimated_remaining_useful_life_hours"]), 1))
    if CRITICALITY_LEVELS.index(data["criticality"]) >= 2:
        data["loto_procedure_required"] = True
    return WorkOrder(**data)


def generate_work_order(
    machine_id: str,
    anomaly: Dict[str, Any],
    inspection: Optional[InspectionReport] = None,
    use_mock: Optional[bool] = None,
    model: str = DEFAULT_MODEL,
) -> Tuple[WorkOrder, str]:
    """Produce a WorkOrder. Returns (work_order, source) with source 'gemini' or 'mock'."""
    mode = "mock" if use_mock else get_llm_mode()
    wo: Optional[WorkOrder] = None
    source = "mock"
    if mode != "mock":
        try:
            wo = _triage_gemini(machine_id, anomaly, inspection, mode, model)
            source = "gemini"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini triage failed (%s); using rule-based engine.", exc)
    if wo is None:
        wo = _triage_mock(machine_id, anomaly, inspection)
    return _apply_guardrails(wo, machine_id, anomaly, inspection), source


def _triage_gemini(machine_id: str, anomaly: Dict[str, Any], inspection: Optional[InspectionReport], mode: str, model: str) -> WorkOrder:
    from google.genai import types

    client = get_genai_client(mode)
    sops = retrieve_context(anomaly, inspection)
    prompt = (
        f"Machine: {machine_id}\n"
        f"Suggested incident_id: {_incident_id(machine_id, anomaly)}\n\n"
        f"## Telemetry anomaly report (JSON)\n{json.dumps(anomaly, indent=2, default=str)}\n\n"
        f"## Visual inspection report (JSON)\n"
        f"{inspection.model_dump_json(indent=2) if inspection else 'No visual inspection available.'}\n\n"
        f"## Retrieved SOPs and parts (JSON)\n{json.dumps(sops, indent=2)}\n\n"
        "Produce the work order."
    )
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            system_instruction=TRIAGE_SYSTEM,
            temperature=0.2,
            response_mime_type="application/json",
            response_schema=WorkOrder,
        ),
    )
    if getattr(response, "parsed", None) is not None and isinstance(response.parsed, WorkOrder):
        return response.parsed
    return WorkOrder.model_validate_json(response.text)


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _triage_mock(machine_id: str, anomaly: Dict[str, Any], inspection: Optional[InspectionReport]) -> WorkOrder:
    """Deterministic rule-based triage mirroring the SOP library."""
    mode = anomaly.get("suspected_mode", "normal")
    status = anomaly.get("status", "HEALTHY")
    snap = anomaly.get("snapshot", {})
    derived = snap.get("derived", {})
    defects = list(inspection.defects) if inspection else []

    def z(ch: str) -> float:
        return abs(float(snap.get(ch, {}).get("z_score", 0.0)))

    parts: Dict[str, int] = {}
    steps: List[str] = []
    causes: List[str] = []
    sop_refs: List[str] = []
    rul = 720.0
    crit_rank = {"HEALTHY": 0, "WARNING": 1, "CRITICAL": 2}.get(status, 0)
    failure_mode = "No active failure mode"

    if mode in SOP_LIBRARY:
        sop = SOP_LIBRARY[mode]
        failure_mode = sop["failure_mode"]
        sop_refs.append(sop["doc"])
        for p in sop["parts"]:
            parts[p] = 2 if p.startswith("SKF") else 1
        steps.extend(sop["steps"])
        v, t, c, a = (snap.get(k, {}) for k in ("vibration_rms", "bearing_temp_c", "motor_current_a", "acoustic_db"))
        if mode == "bearing_inner_race":
            rul = _clamp(sop["typical_rul_hours"] / (1 + z("vibration_rms") / 2.0), 6.0, 168.0)
            causes.append(
                f"Vibration RMS {v.get('recent_mean')} mm/s ({v.get('z_score')}σ above baseline "
                f"{v.get('baseline_mean')}) with impulsive peaks ({derived.get('vibration_peak_to_median')}× median) "
                f"and bearing temperature {t.get('recent_mean')} °C while current is stable "
                f"({c.get('delta_pct')} % vs baseline) is the signature of an inner-race spall per {sop['doc']}."
            )
        elif mode == "thermal_overload":
            h = derived.get("hours_to_temp_critical")
            rul = _clamp(float(h) if h is not None else sop["typical_rul_hours"], 0.5, 72.0)
            causes.append(
                f"Bearing temperature {t.get('recent_mean')} °C (+{t.get('delta_pct')} %, slope "
                f"{derived.get('bearing_temp_slope_c_per_hr')} °C/h) with motor current {c.get('recent_mean')} A "
                f"({c.get('delta_pct')} % vs baseline) and falling shaft speed indicate sustained over-current or "
                f"loss of cooling per {sop['doc']}."
            )
        else:
            rul = _clamp(sop["typical_rul_hours"] / (1 + float(c.get("volatility_ratio", 0)) / 4.0), 8.0, 120.0)
            causes.append(
                f"Motor current variance is {c.get('volatility_ratio')}× baseline and acoustic level is "
                f"{a.get('recent_mean')} dB ({a.get('z_score')}σ), the signature of vapour-bubble collapse at the "
                f"impeller (insufficient NPSH) per {sop['doc']}."
            )

    for d in defects:
        vs = VISUAL_SOPS.get(d.defect_type.value, VISUAL_SOPS["other"])
        sop_refs.append(vs["doc"])
        for p in vs["parts"]:
            parts.setdefault(p, 1)
        for s in vs["steps"]:
            if s not in steps:
                steps.append(s)
        causes.append(
            f"Visual inspection found {d.defect_type.value.replace('_', ' ')} ({d.severity}, "
            f"{d.confidence:.0%} confidence): {d.description}"
        )
        crit_rank = max(crit_rank, SEVERITY_RANK[d.severity] - 1)
        if d.defect_type.value == "hairline_fatigue_crack":
            rul = min(rul, 12.0)
        elif d.severity in ("HIGH", "CRITICAL"):
            rul = min(rul, 72.0)
    if defects and mode == "normal":
        failure_mode = "Visual defect: " + ", ".join(sorted({d.defect_type.value.replace("_", " ") for d in defects}))

    if not causes:
        causes.append("All monitored channels are within baseline and no visual defects were reported; no fault is indicated.")

    if rul < 12.0 and crit_rank >= 1:
        crit_rank = 3  # failure expected within a shift
    criticality = CRITICALITY_LEVELS[int(_clamp(crit_rank, 0, 3))]
    needs_loto = bool(steps) and criticality != "LOW"
    final_steps: List[str] = []
    if needs_loto:
        final_steps = [f"LOTO: {s}" for s in LOTO_STEPS] + steps + RETURN_TO_SERVICE_STEPS
    elif steps:
        final_steps = steps + RETURN_TO_SERVICE_STEPS
    else:
        final_steps = ["Continue normal operation and routine condition-monitoring rounds; no corrective action required."]

    rca = " ".join(causes)
    if sop_refs:
        rca += f" Referenced procedures: {', '.join(dict.fromkeys(sop_refs))}."
    return WorkOrder(
        incident_id=_incident_id(machine_id, anomaly),
        machine_id=machine_id,
        criticality=criticality,
        failure_mode=failure_mode,
        root_cause_analysis=rca,
        estimated_remaining_useful_life_hours=round(rul, 1),
        required_parts=[_part_string(p, q) for p, q in parts.items()],
        loto_procedure_required=needs_loto,
        remediation_steps=final_steps,
    )


# --------------------------------------------------------------------------- #
# Chat copilot
# --------------------------------------------------------------------------- #
CHAT_SYSTEM = """You are FactoryPulse Copilot, a concise, safety-first assistant for field maintenance engineers.
Use the provided machine context and call the tools (lookup_sop, lookup_spare_part, get_machine_status) to ground
answers in SOPs and live data. Always cite SOP numbers and numeric evidence. Never advise bypassing lock-out/tag-out
or safety interlocks. If you are unsure, say so and recommend verification by a qualified technician.
Keep answers under 180 words unless the user asks for detail."""


def chat_reply(
    history: List[Dict[str, str]],
    user_message: str,
    context: Dict[str, Any],
    use_mock: Optional[bool] = None,
    model: str = DEFAULT_MODEL,
) -> Tuple[str, str]:
    """Answer a question about the selected machine. Returns (text, source).

    ``history`` is a list of {"role": "user"|"assistant", "content": str} prior turns.
    ``context`` holds machine_id, anomaly (dict), inspection (dict|None), work_order (dict|None).
    """
    mode = "mock" if use_mock else get_llm_mode()
    if mode != "mock":
        try:
            return _chat_gemini(history, user_message, context, mode, model), "gemini"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Gemini chat failed (%s); using offline copilot.", exc)
    return _chat_mock(user_message, context), "mock"


def _chat_gemini(history: List[Dict[str, str]], user_message: str, context: Dict[str, Any], mode: str, model: str) -> str:
    from google.genai import types

    client = get_genai_client(mode)

    def get_machine_status() -> Dict[str, Any]:
        """Return the current telemetry anomaly report, visual inspection and work order for the selected machine."""
        return json.loads(json.dumps(context, default=str))

    contents = [
        types.Content(role="user" if m["role"] == "user" else "model", parts=[types.Part(text=m["content"])])
        for m in history
    ]
    contents.append(types.Content(role="user", parts=[types.Part(text=user_message)]))
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=CHAT_SYSTEM + f"\n\nSelected machine: {context.get('machine_id')}",
            temperature=0.3,
            tools=[lookup_sop, lookup_spare_part, get_machine_status],  # automatic function calling
        ),
    )
    if not response.text:
        raise RuntimeError("Empty response from Gemini")
    return response.text


def _chat_mock(message: str, context: Dict[str, Any]) -> str:
    q = message.lower()
    anomaly = context.get("anomaly") or {}
    wo = context.get("work_order")
    if wo is None:
        insp = context.get("inspection")
        report = InspectionReport.model_validate(insp) if insp else None
        wo = _triage_mock(context.get("machine_id", "UNKNOWN"), anomaly, report).model_dump()
    machine = context.get("machine_id", "the machine")

    if any(k in q for k in ("loto", "lock", "tag", "isolate", "safe")):
        steps = [s for s in wo["remediation_steps"] if s.startswith("LOTO:")] or LOTO_STEPS
        body = "\n".join(f"{i}. {s.replace('LOTO: ', '')}" for i, s in enumerate(steps, 1))
        return f"Lock-out/tag-out sequence for {machine} (SOP-LOTO-001):\n{body}"
    if any(k in q for k in ("part", "spare", "stock", "order", "bom")):
        if not wo["required_parts"]:
            return f"No spare parts are required for {machine} at this time."
        return "Required parts:\n" + "\n".join(f"- {p}" for p in wo["required_parts"])
    if any(k in q for k in ("how long", "rul", "time", "when", "life", "urgent", "deadline")):
        return (
            f"Estimated remaining useful life for {machine} is about {wo['estimated_remaining_useful_life_hours']} h "
            f"(criticality {wo['criticality']}). Plan the intervention inside that window; reassess if the trend steepens."
        )
    if any(k in q for k in ("why", "cause", "root", "diagnos", "what is wrong", "what's wrong", "happen")):
        return f"**{wo['failure_mode']}** - {wo['root_cause_analysis']}"
    if any(k in q for k in ("step", "fix", "repair", "procedure", "how do", "remed")):
        return "Recommended procedure:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(wo["remediation_steps"], 1))
    return (
        f"{machine} status: **{anomaly.get('status', 'UNKNOWN')}**, health {anomaly.get('health_score', 'n/a')}/100, "
        f"suspected mode: {wo['failure_mode']}. Criticality {wo['criticality']}, RUL ≈ "
        f"{wo['estimated_remaining_useful_life_hours']} h. Ask me about the root cause, required parts, "
        "LOTO steps or the repair procedure. (Offline copilot - set GEMINI_API_KEY or Vertex AI for live Gemini reasoning.)"
    )


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #
def work_order_to_json(wo: WorkOrder, meta: Optional[Dict[str, Any]] = None) -> str:
    payload = {"work_order": wo.model_dump(), "meta": meta or {}}
    return json.dumps(payload, indent=2, default=str)


_PDF_MAP = {"—": "-", "–": "-", "σ": "sigma", "’": "'", "‘": "'", "“": '"', "”": '"', "≥": ">=", "≤": "<=",
            "→": "->", "Ω": "ohm", "≈": "~", "§": "S", "·": "-"}


def _safe(text: str) -> str:
    for k, v in _PDF_MAP.items():
        text = text.replace(k, v)
    return text.encode("latin-1", "replace").decode("latin-1")


_CRIT_RGB = {"LOW": (60, 170, 100), "MEDIUM": (230, 180, 30), "HIGH": (240, 120, 20), "CRITICAL": (215, 40, 50)}


def work_order_to_pdf(wo: WorkOrder, meta: Optional[Dict[str, Any]] = None) -> bytes:
    """Render a clean, printable A4 work order."""
    meta = meta or {}
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(True, margin=15)
    pdf.add_page()

    pdf.set_fill_color(18, 22, 30)
    pdf.rect(0, 0, 210, 30, "F")
    pdf.set_xy(10, 8)
    pdf.set_text_color(255, 176, 0)
    pdf.set_font("Helvetica", "B", 18)
    pdf.cell(0, 8, "FACTORYPULSE  |  MAINTENANCE WORK ORDER", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(10)
    pdf.set_text_color(200, 205, 215)
    pdf.set_font("Helvetica", "", 9)
    generated = meta.get("generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M")
    pdf.cell(0, 6, _safe(f"Generated {generated}  -  Source: {meta.get('source', 'n/a')}"), new_x="LMARGIN", new_y="NEXT")

    pdf.set_y(38)
    pdf.set_text_color(20, 20, 20)

    def kv(key: str, value: str) -> None:
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 10)
        pdf.cell(52, 7, _safe(key))
        pdf.set_font("Helvetica", "", 10)
        pdf.multi_cell(0, 7, _safe(value), new_x="LMARGIN", new_y="NEXT")

    def heading(title: str) -> None:
        pdf.ln(3)
        pdf.set_x(10)
        pdf.set_font("Helvetica", "B", 12)
        pdf.set_text_color(18, 22, 30)
        pdf.cell(0, 8, _safe(title.upper()), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(255, 176, 0)
        pdf.set_line_width(0.6)
        pdf.line(10, pdf.get_y(), 200, pdf.get_y())
        pdf.ln(2)
        pdf.set_text_color(20, 20, 20)

    kv("Incident ID", wo.incident_id)
    kv("Machine ID", wo.machine_id)
    kv("Failure mode", wo.failure_mode)
    pdf.set_x(10)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(52, 8, "Criticality")
    pdf.set_fill_color(*_CRIT_RGB[wo.criticality])
    pdf.set_text_color(255, 255, 255)
    pdf.cell(34, 8, wo.criticality, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")
    pdf.set_text_color(20, 20, 20)
    kv("Est. remaining useful life", f"{wo.estimated_remaining_useful_life_hours:.1f} hours")
    kv("LOTO required", "YES - lock-out/tag-out mandatory" if wo.loto_procedure_required else "No")

    heading("Root cause analysis")
    pdf.set_x(10)
    pdf.set_font("Helvetica", "", 10)
    pdf.multi_cell(0, 6, _safe(wo.root_cause_analysis), new_x="LMARGIN", new_y="NEXT")

    heading("Required parts")
    pdf.set_font("Helvetica", "", 10)
    if wo.required_parts:
        for p in wo.required_parts:
            pdf.set_x(12)
            pdf.multi_cell(0, 6, _safe(f"- {p}"), new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_x(12)
        pdf.cell(0, 6, "None", new_x="LMARGIN", new_y="NEXT")

    heading("Remediation procedure")
    pdf.set_font("Helvetica", "", 10)
    for i, step in enumerate(wo.remediation_steps, 1):
        pdf.set_x(12)
        pdf.multi_cell(0, 6, _safe(f"{i}. {step}"), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(1)

    heading("Sign-off")
    pdf.set_font("Helvetica", "", 10)
    for label in ("Technician", "Supervisor", "Date / time"):
        pdf.set_x(12)
        pdf.cell(40, 12, label)
        pdf.cell(80, 12, "_" * 38, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())
