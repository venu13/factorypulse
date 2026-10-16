# 🏭 FactoryPulse
**Industrial Predictive Maintenance & Visual Inspection Copilot** — Google Cloud hackathon, *Manufacturing: Intelligent Operations & Industrial* track.

FactoryPulse fuses three signals into one maintenance decision:

1. **Telemetry anomaly detection** – synthetic pump/motor data (vibration RMS, bearing temp, motor current, RPM, acoustic dB) with four injectable fault modes, scored by ISO‑10816‑style limits, baseline z‑scores and volatility ratios.
2. **Multimodal visual QA** – Gemini detects, localizes (bounding boxes) and classifies cracks, heat discoloration, pitting, wear, corrosion and weld porosity.
3. **Operational copilot** – Gemini correlates both with SOPs and returns a Pydantic‑validated `WorkOrder` (criticality, RUL, parts, LOTO steps), plus a tool‑calling root‑cause chat. Export to PDF/JSON.

```
telemetry.py  → signal synthesis + AnomalyDetector
inspector.py  → Gemini vision pipeline, annotation, sample-image generator
copilot.py    → SOP/parts knowledge base, structured triage, chat tools, PDF/JSON export
app.py        → Streamlit dashboard (4 tabs)
```

## Offline demo mode
With **no credentials** the app runs fully offline: a deterministic rule engine replaces Gemini and the bundled sample images carry embedded ground truth. A badge in the sidebar shows the active mode. Live Gemini failures also fall back to the mock automatically.

## Run locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
streamlit run app.py          # http://localhost:8501
```

### Option A – Gemini Developer API key
```bash
export GEMINI_API_KEY="your-key"
```

### Option B – Vertex AI (recommended for Cloud Run)
```bash
gcloud auth application-default login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable aiplatform.googleapis.com

export GOOGLE_GENAI_USE_VERTEXAI=true
export GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID
export GOOGLE_CLOUD_LOCATION=us-central1
```

Optional: `GEMINI_MODEL` (default `gemini-2.5-flash`; try `gemini-2.5-pro` for harder images), `FACTORYPULSE_FORCE_MOCK=1` to force offline mode.

## Run with Docker
```bash
docker build -t factorypulse .
docker run --rm -p 8080:8080 factorypulse                       # offline demo
docker run --rm -p 8080:8080 -e GEMINI_API_KEY=... factorypulse # live Gemini
```

## Deploy to Google Cloud Run

```bash
export PROJECT_ID=YOUR_PROJECT_ID REGION=us-central1 SERVICE=factorypulse

gcloud config set project $PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com \
    cloudbuild.googleapis.com aiplatform.googleapis.com

# Dedicated service account with Vertex AI access (no keys needed on Cloud Run)
gcloud iam service-accounts create factorypulse-sa --display-name "FactoryPulse"
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member "serviceAccount:factorypulse-sa@$PROJECT_ID.iam.gserviceaccount.com" \
    --role roles/aiplatform.user

# Build from source and deploy
gcloud run deploy $SERVICE \
    --source . \
    --region $REGION \
    --service-account factorypulse-sa@$PROJECT_ID.iam.gserviceaccount.com \
    --port 8080 \
    --memory 1Gi --cpu 1 \
    --min-instances 0 --max-instances 3 \
    --session-affinity \
    --set-env-vars GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GOOGLE_CLOUD_LOCATION=$REGION \
    --allow-unauthenticated   # remove for private/IAP-protected access
```

`--session-affinity` keeps a browser's Streamlit websocket on the same instance. For a Developer-API key instead of Vertex, store it in Secret Manager and use `--set-secrets GEMINI_API_KEY=gemini-key:latest`.

## Demo script (3 minutes)
1. **System Overview** – fleet with one critical bearing, one thermal overload, one cavitating pump.
2. **Telemetry Explorer** – pick `PUMP-101`, inject *Inner race bearing defect*, toggle *Live stream replay* and watch anomaly markers appear and the detector classify the mode.
3. **Visual QA Lab** – inspect the *weld* sample, attach it to `MTR-201`: Gemini boxes a branching fatigue crack (CRITICAL).
4. **Copilot** – run triage for `MTR-201`: telemetry + crack → CRITICAL work order with part numbers and LOTO steps. Ask "Which parts do I need?" then export the PDF.

## Design notes
- **Structured output vs. tools:** Gemini cannot combine a JSON response schema with function calling in one request, so triage uses `response_schema=WorkOrder` with retrieved SOPs injected into the prompt, while the chat copilot uses automatic function calling (`lookup_sop`, `lookup_spare_part`, `get_machine_status`).
- **Safety guardrails:** after the model responds, deterministic floors prevent under‑rating (e.g. a CRITICAL telemetry status can never yield a LOW work order; HIGH/CRITICAL work forces LOTO).
- Telemetry is simulated; replace `generate_telemetry` with an OPC UA / Pub/Sub / BigQuery reader for production use. The SOP library in `copilot.py` is illustrative — swap in Vertex AI Search / RAG over your real documents.
