"""End-to-end UI smoke tests with Streamlit's AppTest (mock mode, no network)."""
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


@pytest.fixture
def at():
    return AppTest.from_file(APP, default_timeout=120).run()


def _click(at, label):
    [b for b in at.button if label in b.label][0].click()
    return at.run()


def test_loads_without_exceptions(at):
    assert not at.exception
    assert any("Fleet analytics" in h.value for h in at.subheader)


def test_fault_settings_survive_switching_machines(at):
    """Regression: widget keys vanish when not rendered; fault config must persist per machine."""
    at.selectbox(key="explorer_machine").select("MTR-201").run()
    at.radio(key="w_mode_MTR-201").set_value("thermal_overload").run()
    at.selectbox(key="explorer_machine").select("PUMP-301").run()
    at.radio(key="w_mode_PUMP-301").set_value("cavitation").run()
    at.selectbox(key="explorer_machine").select("MTR-201").run()
    assert not at.exception
    assert at.session_state["cfg_mode_MTR-201"] == "thermal_overload"
    assert at.session_state["cfg_mode_PUMP-301"] == "cavitation"
    assert at.radio(key="w_mode_MTR-201").value == "thermal_overload"


def test_inspection_triage_chat_flow_labels_provenance(at):
    at.selectbox(key="cp_machine").select("MTR-202").run()
    _click(at, "Run AI triage")
    rec = at.session_state.work_orders["MTR-202"]
    assert rec["prov"]["label"] == "mock" and rec["pdf"][:4] == b"%PDF"
    assert '"provenance": "mock"' in rec["json"]
    _click(at, "LOTO steps")
    msgs = at.session_state.chats["MTR-202"]
    assert msgs[-1]["prov"]["label"] == "mock" and "Lock-out" in msgs[-1]["content"]
    assert not at.exception


def test_demo_scenario_golden_path(at):
    _click(at, "Run demo scenario")
    assert not at.exception
    assert len(at.session_state.demo_log) == 5
    rec = at.session_state.work_orders["PUMP-101"]
    assert rec["wo"].criticality in ("HIGH", "CRITICAL") and rec["wo"].loto_procedure_required
    assert rec["pdf"][:4] == b"%PDF"
    assert at.session_state["cfg_mode_PUMP-101"] == "bearing_inner_race"
    assert len(at.session_state.store.tables["work_orders"]) == 1


def test_selftest_runs_in_mock_mode(at):
    _click(at, "self-test")
    assert not at.exception


def test_demo_uses_weld_image_and_shows_per_call_progress(at):
    _click(at, "Run demo scenario")
    assert not at.exception
    assert at.session_state["qa_source"] == "weld"
    log = at.session_state.demo_log
    assert "Weld inspection" in log[2] and " s [" in log[2]       # timed
    assert "Work order" in log[3] and " s [" in log[3]
    rec = at.session_state.inspections["PUMP-101"]
    assert rec["report"]["component_type"] == "butt weld seam"


def test_qa_lab_states_localization_is_approximate(at, monkeypatch):
    from inspector import LOCALIZATION_NOTE
    assert "approximate" in LOCALIZATION_NOTE and "not calibrated" in LOCALIZATION_NOTE and "0.45" in LOCALIZATION_NOTE


def test_fleet_card_flips_to_critical_after_demo(at):
    """The fault injected by the demo must show up on the PUMP-101 fleet card in the same run."""
    before = " ".join(m.value for m in at.markdown if "PUMP-101" in m.value and "fp-card" in m.value)
    assert "HEALTHY" in before
    _click(at, "Run demo scenario")
    after = " ".join(m.value for m in at.markdown if "PUMP-101" in m.value and "fp-card" in m.value)
    assert "CRITICAL" in after and "HEALTHY" not in after


def test_healthy_demo_fleet_assets_show_no_warning(at):
    cards = {m: " ".join(x.value for x in at.markdown if m in x.value and "fp-card" in x.value)
             for m in ("PUMP-101", "PUMP-301", "FAN-401")}
    assert all("HEALTHY" in v and "WARNING" not in v for v in cards.values()), cards


def test_validation_panel_does_not_round_recall_up_to_100(at):
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Recall"] == "99.96%", metrics["Recall"]
    assert metrics["Precision"] == "99.0%"
