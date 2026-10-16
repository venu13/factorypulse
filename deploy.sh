#!/usr/bin/env bash
# FactoryPulse -> Google Cloud Run (idempotent). Usage:
#   PROJECT_ID=my-proj REGION=us-central1 ./deploy.sh
#   ./deploy.sh my-proj us-central1
#   DRY_RUN=1 ./deploy.sh my-proj us-central1     # print every mutating command, run nothing, touch nothing
set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
PROJECT_ID="${1:-${PROJECT_ID:-}}"
REGION="${2:-${REGION:-us-central1}}"
SERVICE="${SERVICE:-factorypulse}"
SA_NAME="${SA_NAME:-factorypulse-sa}"
BQ_DATASET="${BQ_DATASET:-factorypulse}"
BQ_LOCATION="${BQ_LOCATION:-US}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-2.5-flash}"
CREATE_PREFLIGHT_JOB="${CREATE_PREFLIGHT_JOB:-1}"

if [[ -z "${PROJECT_ID}" ]]; then
  echo "ERROR: pass PROJECT_ID explicitly (env var or first argument); it is never guessed from gcloud config." >&2
  exit 1
fi
BUCKET="${BUCKET:-${PROJECT_ID}-factorypulse}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# Target this project for every gcloud call in this script WITHOUT changing the user's global gcloud config.
export CLOUDSDK_CORE_PROJECT="${PROJECT_ID}"

log() { printf '\n==> %s\n' "$*"; }
# run: execute a command, or just print it when DRY_RUN=1
run() {
  if [[ "${DRY_RUN}" == "1" ]]; then printf '+ '; printf '%q ' "$@"; printf '\n'; else "$@"; fi
}
# exists: existence probe; in DRY_RUN assume "does not exist" so the create command is shown
exists() { [[ "${DRY_RUN}" != "1" ]] && "$@" >/dev/null 2>&1; }

log "Project=${PROJECT_ID} Region=${REGION} Service=${SERVICE} Model=${GEMINI_MODEL} DRY_RUN=${DRY_RUN}"

log "Enabling APIs (run, aiplatform, artifactregistry, cloudbuild, bigquery, storage)"
run gcloud services enable \
  run.googleapis.com aiplatform.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com bigquery.googleapis.com storage.googleapis.com \
  --project "${PROJECT_ID}"

log "Service account ${SA_EMAIL}"
if ! exists gcloud iam service-accounts describe "${SA_EMAIL}" --project "${PROJECT_ID}"; then
  run gcloud iam service-accounts create "${SA_NAME}" --display-name "FactoryPulse runtime" --project "${PROJECT_ID}"
  if [[ "${DRY_RUN}" != "1" ]]; then echo "created; waiting for IAM propagation..."; sleep 15; fi
fi

log "Project-level roles (aiplatform.user, bigquery.dataEditor, bigquery.jobUser)"
for ROLE in roles/aiplatform.user roles/bigquery.dataEditor roles/bigquery.jobUser; do
  run gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member "serviceAccount:${SA_EMAIL}" --role "${ROLE}" --condition=None --quiet
done

log "Cloud Storage bucket gs://${BUCKET}"
if ! exists gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT_ID}"; then
  run gcloud storage buckets create "gs://${BUCKET}" --project "${PROJECT_ID}" \
    --location "${REGION}" --uniform-bucket-level-access
fi
run gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member "serviceAccount:${SA_EMAIL}" --role roles/storage.objectAdmin

log "BigQuery dataset ${PROJECT_ID}:${BQ_DATASET} (${BQ_LOCATION})"
if ! exists bq --project_id "${PROJECT_ID}" show --dataset "${PROJECT_ID}:${BQ_DATASET}"; then
  run bq --project_id "${PROJECT_ID}" --location "${BQ_LOCATION}" mk --dataset \
    --description "FactoryPulse telemetry, anomalies and work orders" "${PROJECT_ID}:${BQ_DATASET}"
fi

# --set-env-vars REPLACES all env vars on the service, so a stray FACTORYPULSE_FORCE_MOCK can never survive a deploy.
ENV_VARS="GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=${REGION},GEMINI_MODEL=${GEMINI_MODEL}"
ENV_VARS+=",FACTORYPULSE_GCS_BUCKET=${BUCKET},FACTORYPULSE_BQ_DATASET=${BQ_DATASET},FACTORYPULSE_BQ_LOCATION=${BQ_LOCATION}"

# --quiet auto-accepts the one-time "create Artifact Registry repo cloud-run-source-deploy" prompt for source deploys.
log "Deploying Cloud Run service from source (Cloud Build)"
run gcloud run deploy "${SERVICE}" \
  --source . \
  --project "${PROJECT_ID}" --region "${REGION}" \
  --service-account "${SA_EMAIL}" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 2Gi --cpu 2 \
  --timeout 3600 \
  --min-instances 1 --max-instances 5 \
  --session-affinity \
  --set-env-vars "${ENV_VARS}" \
  --quiet

if [[ "${DRY_RUN}" == "1" ]]; then
  URL="<printed by 'gcloud run services describe ${SERVICE}'>"
else
  URL="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" --format 'value(status.url)')"
fi

if [[ "${CREATE_PREFLIGHT_JOB}" == "1" ]]; then
  log "Cloud Run job for in-cloud preflight (same image)"
  if [[ "${DRY_RUN}" == "1" ]]; then
    IMAGE="<image of the deployed revision>"
  else
    IMAGE="$(gcloud run services describe "${SERVICE}" --project "${PROJECT_ID}" --region "${REGION}" \
      --format 'value(spec.template.spec.containers[0].image)')"
  fi
  run gcloud run jobs deploy "${SERVICE}-preflight" \
    --image "${IMAGE}" --project "${PROJECT_ID}" --region "${REGION}" \
    --service-account "${SA_EMAIL}" --max-retries 0 --task-timeout 600 \
    --command python --args scripts/preflight.py \
    --set-env-vars "${ENV_VARS}"
  echo "Run it with: gcloud run jobs execute ${SERVICE}-preflight --region ${REGION} --wait"
fi

log "DONE"
echo "Service URL: ${URL}"
echo "Next: ./scripts/smoke_test.sh ${URL}"
