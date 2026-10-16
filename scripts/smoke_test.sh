#!/usr/bin/env bash
# Smoke-test a deployed FactoryPulse service.
#   ./scripts/smoke_test.sh https://factorypulse-xxxx-uc.a.run.app
# Env: PROJECT_ID, REGION (for the in-cloud preflight job; optional)
set -uo pipefail

URL="${1:-${SERVICE_URL:-}}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-factorypulse}"
if [[ -z "${URL}" ]] && command -v gcloud >/dev/null 2>&1; then
  URL="$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)' 2>/dev/null || true)"
fi
if [[ -z "${URL}" ]]; then echo "usage: $0 <service-url>" >&2; exit 2; fi
URL="${URL%/}"
FAIL=0
pass() { echo "PASS  $*"; }
fail() { echo "FAIL  $*"; FAIL=1; }

# 1. Streamlit health endpoint
HEALTH="$(curl -fsS --max-time 20 "${URL}/_stcore/health" 2>&1)"
[[ "${HEALTH}" == "ok" ]] && pass "/_stcore/health -> ok" || fail "/_stcore/health -> '${HEALTH}'"

# 2. Index page is served
CODE="$(curl -s -o /tmp/fp_index.html -w '%{http_code}' --max-time 20 "${URL}/")"
if [[ "${CODE}" == "200" ]] && grep -qi "streamlit" /tmp/fp_index.html; then pass "GET / -> 200 (Streamlit shell)"; else fail "GET / -> ${CODE}"; fi

# 3. Websocket upgrade (what Streamlit needs behind the Cloud Run proxy)
WS="$(curl -s --http1.1 -i --max-time 6 -o - "${URL}/_stcore/stream" \
  -H 'Connection: Upgrade' -H 'Upgrade: websocket' -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: SGVsbG8sIHdvcmxkIQ==' -H "Origin: ${URL}" 2>/dev/null | head -1)"
[[ "${WS}" == *"101"* ]] && pass "websocket upgrade -> 101" || fail "websocket upgrade -> '${WS:-no response}'"

# 4. Live-Gemini preflight inside Cloud Run (if the job exists and gcloud is authenticated)
if command -v gcloud >/dev/null 2>&1 && gcloud run jobs describe "${SERVICE}-preflight" --region "${REGION}" >/dev/null 2>&1; then
  echo "Running in-cloud preflight job (this makes real Vertex AI calls)..."
  if gcloud run jobs execute "${SERVICE}-preflight" --region "${REGION}" --wait; then
    pass "in-cloud preflight job succeeded (see job logs for the PASS/FAIL table)"
  else
    fail "in-cloud preflight job failed: gcloud run jobs executions list --job ${SERVICE}-preflight --region ${REGION}"
  fi
else
  echo "SKIP  in-cloud preflight (job '${SERVICE}-preflight' not found). Run locally:"
  echo "      GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=<proj> python scripts/preflight.py"
  echo "      or open ${URL} and use the sidebar 'Diagnostics -> Run Gemini self-test'."
fi

echo; [[ ${FAIL} == 0 ]] && echo "SMOKE TEST PASSED" || echo "SMOKE TEST FAILED"
exit ${FAIL}
