# Cloud Run deployment checklist

> **Status:** `deploy.sh` and `scripts/smoke_test.sh` were run for real against project `project-6856615b-d7ea-4a2b-85d`
> (us-central1) on 2026-10-16: deploy succeeded after the build-SA fix below; smoke test and an in-cloud preflight job passed.

## 0. Prerequisites
- A GCP project with **billing enabled** and you as Owner/Editor (or Cloud Run Admin + Service Account Admin + Project IAM Admin).
- `gcloud` and `bq` (part of the Google Cloud SDK) installed.
- If your org restricts public services (`iam.allowedPolicyMemberDomains`), `--allow-unauthenticated` will fail - ask your admin or use a personal project.

## 1. Authenticate
```bash
gcloud auth login
gcloud auth application-default login          # only needed for running preflight on your laptop
export PROJECT_ID=your-project-id
export REGION=us-central1
gcloud config set project $PROJECT_ID
```
Expect: `Updated property [core/project].`

## 2. Run the offline tests (no network, no credentials)
```bash
pip install -r requirements.txt -r requirements-dev.txt
pytest -q
```
Expect: all tests pass.

## 3. Preflight with LIVE Gemini on Vertex AI (from your laptop)
```bash
gcloud services enable aiplatform.googleapis.com
GOOGLE_GENAI_USE_VERTEXAI=true GOOGLE_CLOUD_PROJECT=$PROJECT_ID GOOGLE_CLOUD_LOCATION=$REGION \
  python scripts/preflight.py
```
Expect a table with three rows, each `PASS` and provenance `gemini-live (gemini-2.5-flash)`, then `ALL CHECKS PASSED`.
If a row says `FAIL`, the exact error is printed (typical causes: API not enabled, no `roles/aiplatform.user`,
model not available in the region -> try `GOOGLE_CLOUD_LOCATION=us-central1` or set `GEMINI_MODEL`).
**Do not deploy until this is green** - otherwise the public demo will show `fallback (...)` badges.

## 4. Deploy
Preview every command first (touches nothing): `DRY_RUN=1 ./deploy.sh $PROJECT_ID $REGION`
```bash
chmod +x deploy.sh scripts/smoke_test.sh
./deploy.sh $PROJECT_ID $REGION
```
What it does: enables APIs, creates service account `factorypulse-sa`, grants `aiplatform.user`, `bigquery.dataEditor`,
`bigquery.jobUser` (project) and `storage.objectAdmin` (bucket only), creates bucket `gs://$PROJECT_ID-factorypulse`
and BigQuery dataset `factorypulse`, builds from source with Cloud Build, deploys with 2 CPU / 2 GiB / min 1 instance,
and creates a Cloud Run job `factorypulse-preflight`.
Expect (last lines): `Service [factorypulse] revision [factorypulse-00001-xxx] has been deployed and is serving 100 percent of traffic.`
then `Service URL: https://factorypulse-<hash>-uc.a.run.app`. First build takes ~4-8 minutes.

## 5. Smoke test the public URL
```bash
./scripts/smoke_test.sh <service-url>
```
Expect:
```
PASS  /_stcore/health -> ok
PASS  GET / -> 200 (Streamlit shell)
PASS  websocket upgrade -> 101
PASS  in-cloud preflight job succeeded (see job logs for the PASS/FAIL table)
SMOKE TEST PASSED
```
The in-cloud preflight proves the **Cloud Run service account** can call Vertex AI (a laptop pass does not).

## 6. Click-through (2 minutes, in a private window)
1. Sidebar badge says **VERTEX AI** (not "OFFLINE MOCK") and shows your project/location.
2. Sidebar -> Diagnostics -> *Run Gemini self-test*: three PASS rows.
3. Visual QA Lab -> run inspection: provenance badge `gemini-live (...)`.
4. Copilot -> Run AI triage: badge `gemini-live (...)`; PDF/JSON downloads work.
5. If `FACTORYPULSE_GCS_BUCKET` / `FACTORYPULSE_BQ_DATASET` are set: a `gs://...` path appears under the work order, and
   the Overview "Fleet Analytics" section is labelled **BigQuery** (not "local mode").

## Troubleshooting
| Symptom | Likely cause / fix |
|---|---|
| Blank page / endless "Please wait" | CORS/XSRF flags missing (they are in the Dockerfile `CMD`); or `--session-affinity` removed |
| Badge says `fallback (HTTP 403)` | Service account lacks `roles/aiplatform.user`, or the Vertex AI API is disabled |
| Badge says `fallback (HTTP 404)` | Model name unavailable in that region: set `GEMINI_MODEL` and redeploy |
| Badge says `fallback (HTTP 429 ...)` | Quota; request an increase or retry; the app already retries twice with backoff |
| BigQuery/GCS warning banner | Check SA roles and that `FACTORYPULSE_BQ_DATASET` exists in the same location (`FACTORYPULSE_BQ_LOCATION`) |
| `deploy.sh` fails on `--allow-unauthenticated` | Org policy blocks public access |
| First build: `Error 403 ... does not have storage.objects.get access` | The default compute service account has no roles in a new project. `deploy.sh` now grants it `roles/cloudbuild.builds.builder` (build SA only) before deploying |
