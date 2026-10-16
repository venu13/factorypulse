"""Regression for a bug found on the deployed demo: a healthy asset showed WARNING (43 % of healthy windows
raised a finding because the duty-cycle swing exceeded 3.5 sigma of a short baseline)."""
import pytest

from telemetry import FaultMode, MIN_DEVIATION, analyze_window, generate_telemetry


def test_healthy_windows_raise_no_findings():
    bad = [s for s in range(1000, 1200)
           if analyze_window(generate_telemetry(FaultMode.NORMAL, seed=s))[1].findings]
    assert bad == [], f"false alarms on healthy data for seeds {bad[:5]}"


def test_demo_fleet_healthy_assets_are_healthy():
    for seed in (11, 15, 16):  # PUMP-101, PUMP-301, FAN-401 default seeds
        assert analyze_window(generate_telemetry(FaultMode.NORMAL, seed=seed))[1].status == "HEALTHY"


@pytest.mark.parametrize("mode", [FaultMode.BEARING_INNER_RACE, FaultMode.THERMAL_OVERLOAD, FaultMode.CAVITATION])
@pytest.mark.parametrize("severity", [0.5, 1.0])
def test_faults_still_detected_and_attributed_after_deadband(mode, severity):
    for seed in range(1, 11):
        r = analyze_window(generate_telemetry(mode, seed=seed, severity=severity))[1]
        assert r.status != "HEALTHY" and r.suspected_mode == mode.value, (mode, severity, seed, r.mode_scores)


def test_drift_findings_report_absolute_change_not_just_sigma():
    f = analyze_window(generate_telemetry(FaultMode.THERMAL_OVERLOAD, seed=3))[1].findings
    drift = [x for x in f if x.kind == "drift"]
    assert all(("σ" in x.message and "from healthy baseline" in x.message) for x in drift)


def test_deadband_covers_every_channel():
    assert set(MIN_DEVIATION) == {"vibration_rms", "bearing_temp_c", "motor_current_a", "rpm", "acoustic_db"}
