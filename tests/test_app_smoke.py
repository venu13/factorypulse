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
