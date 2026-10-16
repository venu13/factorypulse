"""Bounding boxes, retry/backoff, validation retry, provenance and safety floors."""
import json
import math

import pytest

from conftest import FakeAPIError, FakeClient
from copilot import CRITICALITY_LEVELS, WorkOrder, _apply_guardrails, generate_work_order, chat_reply
from inspector import (Defect, InspectionReport, annotate_image, box_to_pixels, inspect_image,
                       make_sample_image, normalize_box)
from llm import LLMCallError, Provenance, generate_with_retry, is_retryable, llm_status
from telemetry import FaultMode, analyze_window, generate_telemetry


# ---- bounding boxes ------------------------------------------------------- #
def test_box_to_pixels_known_values():
    # [ymin, xmin, ymax, xmax] on a 1000 wide x 500 high image
    assert box_to_pixels([100, 200, 500, 600], 1000, 500) == (200, 50, 600, 250)


def test_box_to_pixels_scales_axes_independently():
    x0, y0, x1, y1 = box_to_pixels([0, 0, 1000, 500], 2000, 100)
    assert (x0, y0) == (0, 0) and x1 == 1000 and y1 == 99  # y clamped to last pixel row


def test_box_clamped_to_image():
    assert box_to_pixels([-50, -10, 1200, 5000], 640, 480) == (0, 0, 639, 479)


def test_box_reordered_when_min_max_swapped():
    assert normalize_box([500, 600, 100, 200]) == [100, 200, 500, 600]


@pytest.mark.parametrize("bad", [None, [], [1, 2, 3], [1, 2, 3, 4, 5], "abc", ["a", "b", "c", "d"],
                                 [math.nan, 1, 2, 3], [0, 0, math.inf, 5], [10, 10, 10, 500], {"y": 1}])
def test_malformed_boxes_do_not_crash(bad):
    assert normalize_box(bad) is None
    assert box_to_pixels(bad, 800, 600) is None


@pytest.mark.parametrize("bad", [None, [1, 2, 3], "abc", ["a", "b", "c", "d"], [10, 10, 10, 500]])
def test_malformed_box_becomes_none_in_schema(bad):
    d = Defect(defect_type="surface_pitting", severity="LOW", confidence=0.5, box_2d=bad,
               description="x", recommended_action="y")
    assert d.box_2d is None


def test_annotate_skips_unboxed_defects():
    img = make_sample_image("weld")
    report = InspectionReport(component_type="weld", overall_condition="FAIL", quality_score=50, summary="s", defects=[
        Defect(defect_type="hairline_fatigue_crack", severity="HIGH", confidence=0.9, box_2d=[1, 2, 3],
               description="d", recommended_action="a")])
    assert annotate_image(img, report).size == (1024, 768)


def test_float_boxes_from_model_are_rounded():
    assert normalize_box([10.4, 20.6, 300.2, 400.9]) == [10, 21, 300, 401]


# ---- retry / backoff ------------------------------------------------------ #
def test_is_retryable():
    assert is_retryable(FakeAPIError(429)) and is_retryable(FakeAPIError(503))
    assert not is_retryable(FakeAPIError(400)) and not is_retryable(FakeAPIError(403))
    assert is_retryable(TimeoutError())


def test_retry_succeeds_after_429s_with_exponential_backoff():
    sleeps = []
    client = FakeClient([FakeAPIError(429), FakeAPIError(503), "ok"])
    resp, attempts = generate_with_retry(client, model="m", contents="c", config=None, sleep=sleeps.append)
    assert resp.text == "ok" and attempts == 3 and sleeps == [1.0, 2.0]


def test_retry_gives_up_after_two_retries():
    client = FakeClient([FakeAPIError(429, "quota")] * 3)
    with pytest.raises(LLMCallError) as ei:
        generate_with_retry(client, model="m", contents="c", config=None, sleep=lambda s: None)
    assert ei.value.attempts == 3 and "429" in ei.value.reason and "quota" in ei.value.detail


def test_non_retryable_fails_immediately():
    client = FakeClient([FakeAPIError(400, "bad request")])
    with pytest.raises(LLMCallError) as ei:
        generate_with_retry(client, model="m", contents="c", config=None, sleep=lambda s: None)
    assert ei.value.attempts == 1 and len(client.calls) == 1


# ---- provenance ----------------------------------------------------------- #
def test_provenance_labels():
    assert Provenance.mock("x").label == "mock"
    assert Provenance("gemini-live", model="gemini-2.5-flash").label == "gemini-live (gemini-2.5-flash)"
    assert Provenance("fallback", reason="HTTP 429 rate limited").label == "fallback (HTTP 429 rate limited)"


def test_mock_mode_labelled_mock():
    report, prov = inspect_image(make_sample_image("gear"))
    assert prov.kind == "mock" and prov.label == "mock" and report.defects


GOOD_INSPECTION = json.dumps({"component_type": "gear", "overall_condition": "FAIL", "quality_score": 40,
                              "summary": "s", "defects": [{"defect_type": "surface_pitting", "severity": "HIGH",
                              "confidence": 0.8, "box_2d": [100, 100, 300, 300], "description": "d",
                              "recommended_action": "a"}]})


def test_live_inspection_provenance(live_env, patch_client):
    patch_client(FakeClient([GOOD_INSPECTION]))
    report, prov = inspect_image(make_sample_image("gear"))
    assert prov.label == "gemini-live (gemini-test-model)" and prov.attempts == 1 and report.defects[0].box_2d == [100, 100, 300, 300]


def test_inspection_fallback_is_never_silent(live_env, patch_client):
    patch_client(FakeClient([FakeAPIError(503, "backend down")] * 3))
    report, prov = inspect_image(make_sample_image("gear"), sleep=lambda s: None)
    assert prov.kind == "fallback" and prov.label.startswith("fallback (") and "backend down" in prov.error
    assert prov.attempts == 3 and report.defects  # the mock result is still usable


def test_inspection_bad_schema_falls_back(live_env, patch_client):
    patch_client(FakeClient(["not json at all"]))
    _, prov = inspect_image(make_sample_image("gear"), sleep=lambda s: None)
    assert prov.kind == "fallback" and "validation" in prov.reason


# ---- triage: validation retry + floors ------------------------------------ #
def _anomaly(mode=FaultMode.BEARING_INNER_RACE):
    return analyze_window(generate_telemetry(mode))[1].to_dict()


def _wo_json(**over):
    d = dict(incident_id="INC-1", machine_id="X", criticality="low", failure_mode="f", root_cause_analysis="r",
             estimated_remaining_useful_life_hours=5.0, required_parts=["P-1"], loto_procedure_required=False,
             remediation_steps=["a"])
    d.update(over)
    return json.dumps(d)


def test_triage_validation_retry_appends_error(live_env, patch_client):
    bad = _wo_json(estimated_remaining_useful_life_hours="soon")
    client = patch_client(FakeClient([bad, _wo_json()]))
    wo, prov = generate_work_order("PUMP-1", _anomaly(), sleep=lambda s: None)
    assert prov.kind == "gemini-live" and len(client.calls) == 2
    assert "failed validation" in client.calls[1]["contents"] and "estimated_remaining_useful_life_hours" in client.calls[1]["contents"]
    assert wo.machine_id == "PUMP-1"


def test_triage_falls_back_after_second_validation_failure(live_env, patch_client):
    patch_client(FakeClient(["{}", "{}"]))
    wo, prov = generate_work_order("PUMP-1", _anomaly(), sleep=lambda s: None)
    assert prov.kind == "fallback" and "WorkOrder validation" in prov.reason and prov.error
    assert wo.criticality in CRITICALITY_LEVELS


def test_triage_rejects_invalid_criticality(live_env, patch_client):
    patch_client(FakeClient([_wo_json(criticality="SEVERE"), _wo_json(criticality="SEVERE")]))
    _, prov = generate_work_order("PUMP-1", _anomaly(), sleep=lambda s: None)
    assert prov.kind == "fallback"


def test_safety_floor_overrides_low_model_output(live_env, patch_client):
    patch_client(FakeClient([_wo_json(criticality="LOW", loto_procedure_required=False)]))
    anomaly = _anomaly()
    assert anomaly["status"] == "CRITICAL"
    wo, prov = generate_work_order("PUMP-1", anomaly, sleep=lambda s: None)
    assert prov.kind == "gemini-live"
    assert CRITICALITY_LEVELS.index(wo.criticality) >= 2 and wo.loto_procedure_required
    assert any("criticality raised from LOW" in n for n in prov.notes)
    assert any("lock-out/tag-out forced" in n for n in prov.notes)


def test_floor_adds_no_note_when_model_is_correct():
    wo = WorkOrder.model_validate_json(_wo_json(criticality="CRITICAL", loto_procedure_required=True))
    fixed, notes = _apply_guardrails(wo, "M", _anomaly(), None)
    assert notes == [] and fixed.criticality == "CRITICAL"


# ---- chat ----------------------------------------------------------------- #
def test_chat_fallback_provenance(live_env, patch_client):
    patch_client(FakeClient([FakeAPIError(500, "internal")] * 3))
    text, prov = chat_reply([], "why?", {"machine_id": "M", "anomaly": _anomaly()}, sleep=lambda s: None)
    assert prov.kind == "fallback" and "internal" in prov.error and text


def test_cloud_run_never_defaults_to_mock(monkeypatch):
    monkeypatch.delenv("FACTORYPULSE_FORCE_MOCK", raising=False)
    monkeypatch.setenv("K_SERVICE", "factorypulse")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "demo-proj")
    st = llm_status()
    assert st["mode"] == "vertex" and st["project"] == "demo-proj"
