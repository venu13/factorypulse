"""Test configuration: everything runs offline, in mock mode, with no network."""
import os
import sys
from pathlib import Path

os.environ["FACTORYPULSE_FORCE_MOCK"] = "1"
for var in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_GENAI_USE_VERTEXAI", "K_SERVICE",
            "FACTORYPULSE_GCS_BUCKET", "FACTORYPULSE_BQ_DATASET"):
    os.environ.pop(var, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402


class FakeAPIError(Exception):
    """Mimics google.genai.errors.APIError (has an integer .code)."""

    def __init__(self, code: int, message: str = "boom") -> None:
        super().__init__(f"{code} {message}")
        self.code = code


class FakeResponse:
    def __init__(self, text: str) -> None:
        self.text = text
        self.automatic_function_calling_history = []


class FakeClient:
    """Plays back a script of responses/exceptions for models.generate_content."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        outer = self

        class _Models:
            def generate_content(self, model, contents, config):
                outer.calls.append({"model": model, "contents": contents})
                item = outer.script.pop(0)
                if isinstance(item, BaseException):
                    raise item
                return FakeResponse(item)

        self.models = _Models()


@pytest.fixture
def live_env(monkeypatch):
    """Pretend credentials exist so the live code path is taken (client is patched per test)."""
    monkeypatch.delenv("FACTORYPULSE_FORCE_MOCK", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")


@pytest.fixture
def patch_client(monkeypatch):
    def _patch(client):
        import copilot
        import inspector

        monkeypatch.setattr(inspector, "get_genai_client", lambda mode=None: client)
        monkeypatch.setattr(copilot, "get_genai_client", lambda mode=None: client)
        return client

    return _patch
