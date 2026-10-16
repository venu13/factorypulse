#!/usr/bin/env bash
# FactoryPulse -> Google Cloud Run (idempotent). Usage:
#   PROJECT_ID=my-proj REGION=us-central1 ./deploy.sh
#   ./deploy.sh my-proj us-central1
set -euo pipefail

PROJECT_ID="${1:-${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}}"
REGION="${2:-${REGION:-us-central1}}"
SERVICE="${SERVICE:-factorypulse}"
SA_NAME="${SA_NAME:-factorypulse-sa}"
BUCKET="${BUCKET:-${PROJECT_ID}-factorypulse}"
BQ_DATASET="${BQ_DATASET:-factorypulse}"
BQ_LOCATION="${BQ_LOCATION:-US}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
CREATE_PREFLIGHT_JOB="${CREATE_PREFLIGHT_JOB:-1}"

if [[ -z "${PROJECT_ID}" || "${PROJECT_ID}" == "(unset)" ]]; then
  echo "ERROR: set PROJECT_ID (env var or first argument)." >&2
  exit 1
fi
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

log() { printf '\n==> %s\n' "$*"; }

log "Project=${PROJECT_ID} Region=${REGION} Service=${SERVICE} Model=${GEMINI_MODEL}"
gcloud config set project "${PROJECT_ID}" >/dev/null

log "Enabling APIs (run, aiplatform, artifactregistry, cloudbuild, bigquery, storage)"
gcloud services enable \
  run.googleapis.com aiplatform.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com bigquery.googleapis.com storage.googleapis.com \
  --project "${PROJECT_ID}"

log "Service account ${SA_EMAIL}"
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" --display-name "FactoryPulse runtime" --project "${PROJECT_ID}"
  echo "created; waiting for IAM propagation..." && sleep 10
fi

log "Project-level roles (aiplatform.user, bigquery.dataEditor, bigquery.jobUser)"
for ROLE in roles/aiplatform.user roles/bigquery.dataEditor roles/bigquery.jobUser; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SA_EMAIL}" --role "${ROLE}" --condition=None --quiet >/dev/null
  echo "granted ${ROLE}"
done

log "Cloud Storage bucket gs://${BUCKET}"
if ! gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project "${PROJECT_ID}" \
    --location "${REGION}" --uniform-bucket-level-access
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin >/dev/null
echo "granted roles/storage.objectAdmin on gs://${BUCKET}"

log "BigQuery dataset ${PROJECT_ID}:${BQ_DATASET} (${BQ_LOCATION})"
if ! bq --project_id "${PROJECT_ID}" show --dataset "${PROJECT_ID}:${BQ_DATASET}" >/dev/null 2>&1; then
  bq --project_id "${PROJECT_ID}" --location "${BQ_LOCATION}" mk --dataset \
    --description "FactoryPulse telemetry, anomalies and work orders" "${PROJECT_ID}:${BQ_DATASET}"
fi

# --set-env-vars REPLACES all env vars on the service, so a stray FACTORYPULSE_FORCE_MOCK can never survive a deploy.
ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GEMINI_MODEL=${GEMINI_MODEL}"
ENV_VARS+=",FACTORYPULSE_GCS_BUCKET=${BUCKET},FACTORYPULSE_BQ_DATASET=${BQ_DATASET},FACTORYPULSE_BQ_LOCATION=${BQ_LOCATION}"

log "Deploying Cloud Run service from source (Cloud Build)"
gcloud run deploy "${SERVICE}" \
  --source . \
  --project "${PROJECT_ID}" --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi --cpu 2 \
  --timeout 3600 \
  --min-instances 1 --max-instances 5 \
  --session-affinity \
  --set-env-vars "${ENV_VARS}"

URL="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format 'value(status.url)')"

if [[ "${CREATE_PREFLIGHT_JOB}" == "1" ]]; then
  log "Cloud Run job for in-cloud preflight (same image)"
  IMAGE="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
    --format 'value(spec.template.spec.containers[0].image)')"
  gcloud run jobs deploy "${SERVICE}-preflight" \
    --image "${IMAGE}" --project "${PROJECT_ID}" --region "${REGION}" \
    --service-account "${SA_EMAIL}" --max-retries 0 --task-timeout 600 \
    --command python --args scripts/preflight.py \
    --set-env-vars "${ENV_VARS}"
  echo "Run it with: gcloud run jobs execute ${SERVICE}-preflight --region ${REGION} --wait"
fi

log "DONE"
echo "Service URL: ${URL}"
echo "Next: ./scripts/smoke_test.sh ${URL}"
