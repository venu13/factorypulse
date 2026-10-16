"""Validation pipeline (SYNTHETIC arrays - not real results), impact maths and gallery listing."""
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from impact import ImpactAssumptions, estimate_impact
from inspector import list_sample_gallery

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("validate_real", ROOT / "scripts" / "validate_real_bearing_data.py")
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


def test_evaluate_stream_on_clearly_separable_synthetic_data():
    rng = np.random.default_rng(0)
    base = 0.1 + rng.normal(0, 0.005, 75)
    held = 0.1 + rng.normal(0, 0.005, 60)
    fault = 0.5 + rng.normal(0, 0.05, 80)
    r = validate.evaluate_stream(base, held, fault, n_heldout_same_load=60)
    assert r["recall"] == 1.0 and r["fn"] == 0 and r["fp_same_load"] == 0
    assert r["precision"] == 1.0 and r["detection_delay_windows"] == 0


def test_evaluate_stream_flags_nothing_when_no_fault_is_present():
    rng = np.random.default_rng(1)
    base = 0.1 + rng.normal(0, 0.005, 75)
    r = validate.evaluate_stream(base, 0.1 + rng.normal(0, 0.005, 60), 0.1 + rng.normal(0, 0.005, 80))
    assert r["recall"] < 0.1  # nothing abnormal in the "fault" segment, so (almost) nothing should be flagged


def test_cross_load_false_alarms_are_counted_separately():
    rng = np.random.default_rng(2)
    base = 0.1 + rng.normal(0, 0.005, 75)
    same = 0.1 + rng.normal(0, 0.005, 30)
    other = 0.3 + rng.normal(0, 0.005, 30)  # a different load -> level shift
    r = validate.evaluate_stream(base, np.concatenate([same, other]), 0.6 + rng.normal(0, 0.05, 40), n_heldout_same_load=30)
    assert r["fp_same_load"] == 0 and r["fp_other_load"] > 20


def test_aggregate_micro_averages():
    rows = [dict(tp=8, fp=2, fn=0, tn=10, detection_delay_windows=0, fp_same_load=0, heldout_same_load=5,
                 fp_other_load=2, heldout_other_load=7),
            dict(tp=2, fp=0, fn=2, tn=10, detection_delay_windows=None, fp_same_load=0, heldout_same_load=5,
                 fp_other_load=0, heldout_other_load=7)]
    a = validate.aggregate(rows)
    assert a["precision"] == pytest.approx(10 / 12) and a["recall"] == pytest.approx(10 / 12)
    assert a["streams_with_detection"] == 1 and a["streams"] == 2


def test_window_rms_known_values():
    x = np.concatenate([np.full(600, 2.0), np.full(600, 4.0), np.full(10, 9.0)])
    np.testing.assert_allclose(validate.window_rms(x, fs=12000, window_s=0.05), [2.0, 4.0])


def test_committed_validation_report_is_well_formed():
    path = ROOT / "reports" / "validation_cwru.json"
    if not path.exists():
        pytest.skip("validation report not generated")
    v = json.loads(path.read_text())
    a = v["aggregate"]
    assert a["tp"] + a["fn"] > 0 and 0 <= a["precision"] <= 1 and v["limitations"]
    assert len(v["streams"]) == a["streams"] == 12


def test_impact_is_assumption_driven_and_gated_by_lead_time():
    st = {"A": "CRITICAL", "B": "WARNING", "C": "HEALTHY"}
    a = ImpactAssumptions(downtime_cost_per_hour=1000, unplanned_failure_downtime_h=10, planned_repair_downtime_h=2,
                          p_failure_prevented=0.5, detection_lead_time_h=24, required_planning_window_h=8)
    out = estimate_impact(st, a)
    assert [r["machine"] for r in out["rows"]] == ["A", "B"]
    assert out["total_hours"] == 8.0 and out["total_cost"] == 8000.0  # (10-2)*0.5*2 assets
    a.detection_lead_time_h = 4  # crew cannot plan in time -> nothing credited
    assert estimate_impact(st, a)["total_cost"] == 0.0


def test_gallery_lists_only_images(tmp_path):
    Image.new("RGB", (4, 4)).save(tmp_path / "b.png")
    Image.new("RGB", (4, 4)).save(tmp_path / "a.jpg")
    (tmp_path / "README.md").write_text("x")
    assert list_sample_gallery(tmp_path) == ["a.jpg", "b.png"]
    assert list_sample_gallery(tmp_path / "missing") == []
