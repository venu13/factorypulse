"""Mock mode must never win when real credentials/Vertex settings are present."""
import pytest
from streamlit.testing.v1 import AppTest

import llm
from llm import llm_status


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in ("FACTORYPULSE_FORCE_MOCK", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI",
              "GOOGLE_CLOUD_PROJECT", "GCLOUD_PROJECT", "K_SERVICE"):
        monkeypatch.delenv(v, raising=False)
    llm._adc_project.cache_clear()
    monkeypatch.setattr(llm, "_adc_project", lambda: None)  # never touch real ADC in tests


def test_no_credentials_is_mock_with_reason():
    st = llm_status()
    assert st["mode"] == "mock" and "no credentials" in st["reason"]


def test_vertex_env_selects_vertex(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "europe-west4")
    st = llm_status()
    assert (st["mode"], st["project"], st["location"]) == ("vertex", "p1", "europe-west4")


def test_vertex_requested_without_project_is_mock_but_says_why(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    st = llm_status()
    assert st["mode"] == "mock" and "no GCP project" in st["reason"]


def test_api_key_selects_gemini_api(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x")
    assert llm_status()["mode"] == "gemini-api"


def test_force_mock_wins_only_when_explicitly_set(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    assert llm_status()["mode"] == "vertex"
    monkeypatch.setenv("FACTORYPULSE_FORCE_MOCK", "1")
    assert llm_status()["mode"] == "mock"


def test_cloud_run_resolves_vertex_even_without_vertex_flag(monkeypatch):
    monkeypatch.setenv("K_SERVICE", "factorypulse")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    assert llm_status()["mode"] == "vertex"


def test_ui_shows_vertex_badge_not_mock_when_vertex_env_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "p1")
    from pathlib import Path
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=90).run()
    assert not at.exception
    sidebar_html = " ".join(m.value for m in at.sidebar.markdown)
    assert "VERTEX AI" in sidebar_html and "OFFLINE MOCK" not in sidebar_html
    assert at.sidebar.toggle[0].value is False  # 'Force offline demo mode' is off by default


def test_ui_shows_mock_badge_when_nothing_configured():
    from pathlib import Path
    at = AppTest.from_file(str(Path(__file__).resolve().parent.parent / "app.py"), default_timeout=90).run()
    assert "OFFLINE MOCK" in " ".join(m.value for m in at.sidebar.markdown)
