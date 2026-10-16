"""Shared Gemini / Vertex AI configuration, provenance tracking and resilient calling.

Every LLM-backed result in FactoryPulse carries a :class:`Provenance` so that a fallback
to the offline engine is always visible to the user and never silent.
"""
from __future__ import annotations

import functools
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("factorypulse.llm")

_TRUE = {"1", "true", "yes", "on"}
RETRYABLE_HTTP = {429, 500, 502, 503, 504}


def get_model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


def get_timeout_s() -> float:
    try:
        return float(os.getenv("FACTORYPULSE_LLM_TIMEOUT_S", "60"))
    except ValueError:
        return 60.0


def on_cloud_run() -> bool:
    """Cloud Run sets K_SERVICE for every revision."""
    return bool(os.getenv("K_SERVICE"))


# --------------------------------------------------------------------------- #
# Mode / credentials resolution
# --------------------------------------------------------------------------- #
@functools.lru_cache(maxsize=1)
def _adc_project() -> Optional[str]:
    """Project from Application Default Credentials / metadata server (cached)."""
    try:
        import google.auth

        _, project = google.auth.default()
        return project
    except Exception as exc:  # noqa: BLE001
        logger.info("ADC project lookup failed: %s", exc)
        return None


def resolve_project() -> Optional[str]:
    return os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCLOUD_PROJECT") or _adc_project()


def get_location() -> str:
    return os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")


def llm_status() -> Dict[str, Any]:
    """Resolve which backend is active and why. mode is 'gemini-api' | 'vertex' | 'mock'."""
    model = get_model()
    if os.getenv("FACTORYPULSE_FORCE_MOCK", "").lower() in _TRUE:
        return {"mode": "mock", "reason": "FACTORYPULSE_FORCE_MOCK is set", "model": model}
    vertex_requested = os.getenv("GOOGLE_GENAI_USE_VERTEXAI", "").lower() in _TRUE
    # Cloud Run is always treated as Vertex AI unless an API key is given explicitly,
    # so a missing env var can never silently put the public demo into mock mode.
    if on_cloud_run() and not (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")):
        vertex_requested = True
    if vertex_requested:
        project = resolve_project()
        if project:
            return {"mode": "vertex", "reason": "Vertex AI", "model": model, "project": project, "location": get_location()}
        return {"mode": "mock", "reason": "Vertex AI requested but no GCP project could be resolved", "model": model}
    if os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"):
        return {"mode": "gemini-api", "reason": "Gemini API key", "model": model}
    return {"mode": "mock", "reason": "no credentials configured", "model": model}


def get_llm_mode() -> str:
    return llm_status()["mode"]


def get_genai_client(mode: Optional[str] = None):
    """Build a google-genai client (with HTTP timeout) for the resolved mode, or None for mock."""
    status = llm_status()
    mode = mode or status["mode"]
    if mode == "mock":
        return None
    from google import genai
    from google.genai import types

    http_options = types.HttpOptions(timeout=int(get_timeout_s() * 1000))
    if mode == "gemini-api":
        return genai.Client(api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"), http_options=http_options)
    return genai.Client(
        vertexai=True,
        project=status.get("project") or resolve_project(),
        location=get_location(),
        http_options=http_options,
    )


# --------------------------------------------------------------------------- #
# Provenance
# --------------------------------------------------------------------------- #
@dataclass
class Provenance:
    kind: str  # "gemini-live" | "fallback" | "mock"
    model: str = ""
    reason: str = ""  # short, human-readable
    error: str = ""  # full exception text for fallbacks
    latency_ms: int = 0
    attempts: int = 0
    tool_calls: int = 0
    notes: List[str] = field(default_factory=list)  # e.g. safety-floor overrides

    @property
    def label(self) -> str:
        if self.kind == "gemini-live":
            return f"gemini-live ({self.model})"
        if self.kind == "fallback":
            return f"fallback ({self.reason})"
        return "mock"

    @property
    def is_live(self) -> bool:
        return self.kind == "gemini-live"

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["label"] = self.label
        return d

    @classmethod
    def live(cls, model: str, started: float, attempts: int, tool_calls: int = 0) -> "Provenance":
        return cls("gemini-live", model=model, latency_ms=int((time.time() - started) * 1000), attempts=attempts,
                   tool_calls=tool_calls)

    @classmethod
    def mock(cls, reason: str = "") -> "Provenance":
        return cls("mock", reason=reason or llm_status()["reason"])

    @classmethod
    def fallback(cls, err: "LLMCallError", model: str, started: float) -> "Provenance":
        return cls("fallback", model=model, reason=err.reason, error=err.detail,
                   latency_ms=int((time.time() - started) * 1000), attempts=err.attempts)


class LLMCallError(Exception):
    """A Gemini call that ultimately failed. ``reason`` is short; ``detail`` is the raw message."""

    def __init__(self, reason: str, detail: str, attempts: int = 1) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail, self.attempts = reason, detail, attempts


def _status_code(exc: BaseException) -> Optional[int]:
    code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    if isinstance(code, int):
        return code
    resp = getattr(exc, "response", None)
    code = getattr(resp, "status_code", None)
    return code if isinstance(code, int) else None


def is_retryable(exc: BaseException) -> bool:
    code = _status_code(exc)
    if code is not None:
        return code in RETRYABLE_HTTP
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    try:
        import httpx

        # TransportError covers timeouts, connect/read errors, proxy errors and protocol errors: all transient.
        return isinstance(exc, httpx.TransportError)
    except ImportError:  # pragma: no cover
        return False


def short_reason(exc: BaseException) -> str:
    code = _status_code(exc)
    if code == 429:
        return "HTTP 429 rate limited"
    if code is not None:
        return f"HTTP {code}"
    name = type(exc).__name__
    if "timeout" in name.lower() or isinstance(exc, TimeoutError):
        return "timeout"
    return name


def generate_with_retry(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: Any,
    retries: int = 2,
    base_delay: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> Tuple[Any, int]:
    """Call ``client.models.generate_content`` with a timeout (set on the client), up to
    ``retries`` retries on 429/5xx/timeouts using exponential backoff.

    Returns ``(response, attempts)`` or raises :class:`LLMCallError`.
    """
    last: Optional[BaseException] = None
    for attempt in range(retries + 1):
        try:
            return client.models.generate_content(model=model, contents=contents, config=config), attempt + 1
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt < retries and is_retryable(exc):
                delay = base_delay * (2**attempt)
                logger.warning("Gemini call failed (%s); retry %d/%d in %.1fs", short_reason(exc), attempt + 1, retries, delay)
                sleep(delay)
                continue
            raise LLMCallError(short_reason(exc), f"{type(exc).__name__}: {exc}", attempt + 1) from exc
    raise LLMCallError("unknown", str(last), retries + 1)  # pragma: no cover
