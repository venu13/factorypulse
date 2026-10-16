# syntax=docker/dockerfile:1.7
# ---- Stage 1: build the virtualenv ---------------------------------------------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---- Stage 2: slim, non-root runtime -------------------------------------------
FROM python:3.12-slim AS runtime

# NOTE: FACTORYPULSE_FORCE_MOCK is deliberately NOT set: the container must use live Gemini
# on Vertex AI whenever it runs on Cloud Run (K_SERVICE is set by the platform).
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    STREAMLIT_SERVER_HEADLESS=true \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false

RUN useradd --create-home --uid 10001 appuser
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY --chown=appuser:appuser *.py ./
COPY --chown=appuser:appuser scripts ./scripts
COPY --chown=appuser:appuser sample_images ./sample_images
COPY --chown=appuser:appuser reports ./reports
COPY --chown=appuser:appuser .streamlit ./.streamlit
USER appuser

EXPOSE 8080

# Streamlit exposes /_stcore/health (returns "ok"); works with any $PORT.
HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:%s/_stcore/health' % os.environ.get('PORT','8080'),timeout=3)"

# Cloud Run injects $PORT (8080). Shell form so ${PORT} expands; `exec` keeps streamlit as PID 1
# so SIGTERM from Cloud Run shuts it down cleanly.
# CORS/XSRF protection are disabled because the Cloud Run front end proxies the websocket from
# a different origin than the container sees; without this the app shows a blank page / disconnects.
CMD exec streamlit run app.py \
    --server.port=${PORT} \
    --server.address=0.0.0.0 \
    --server.headless=true \
    --server.enableCORS=false \
    --server.enableXsrfProtection=false \
    --server.fileWatcherType=none
