# FactoryPulse - submission draft

**Track:** Manufacturing - Intelligent Operations & Industrial Efficiency
**Live demo:** https://factorypulse-gnbcujjj5q-uc.a.run.app · **Repo:** https://github.com/venu13/factorypulse · **Video:** `<fill in>`

## Title
FactoryPulse: Industrial Predictive Maintenance & Visual Inspection Copilot

## Tagline
Telemetry, a photo and the SOP binder in; a safety-checked, auditable maintenance work order out - powered by Gemini on Vertex AI.

## Problem statement
Unplanned downtime on rotating equipment and quality escapes from missed cracks are two of the costliest problems on a plant floor, and they are usually handled by separate people and separate tools. A vibration alarm, a technician's photo of a weld and a paper SOP rarely meet in one place, so triage is slow, depends on tribal knowledge, and is hard to audit. AI that does help also has a trust problem: when a model call silently fails or is replaced by a canned answer, engineers cannot tell what they are looking at.

## Solution (about 250 words)
FactoryPulse correlates three signals into one work order. A telemetry engine monitors vibration, bearing temperature, motor current, speed and acoustics, and flags drift with absolute limits, baseline z-scores and a volatility ratio, then attributes the likely failure mode (inner-race bearing defect, thermal overload, cavitation). A vision inspector sends component photos to Gemini, which returns defects with severities and bounding boxes under a strict schema. The copilot then retrieves the relevant SOPs and part numbers and asks Gemini for a schema-validated `WorkOrder`: criticality, estimated remaining useful life, required parts, and a lock-out/tag-out repair procedure. A conversational copilot answers follow-ups by calling SOP and spare-part lookup tools.

Trust is built in rather than bolted on. Every result carries a provenance badge - `gemini-live (model)`, `fallback (reason)` with the exact error, or `mock` - so a failure is never silent, and the label is embedded in exported PDF and JSON. Deterministic safety floors stop the model from under-rating a critical fault or skipping LOTO, and any override is shown. Outputs are validated against Pydantic schemas with a corrective retry. One click on the Demo Scenario button runs the whole path: inject a fault, see the anomaly, inspect a weld image, generate the work order, export the PDF. The app runs on Cloud Run, optionally stores artifacts in Cloud Storage and logs to BigQuery for fleet analytics, and also runs fully offline for development.

## How Gemini on Vertex AI is used
- **Multimodal inspection:** `gemini-2.5-flash` via the `google-genai` SDK (`vertexai=True`) receives the image with a Level-II NDT prompt and a Pydantic `response_schema`; boxes are returned as `[ymin, xmin, ymax, xmax]` on a 0-1000 grid, normalised and clamped before drawing. Extended "thinking" is switched off for this call because it measurably improved localization and halved latency.
- **Structured triage:** retrieved SOPs, the anomaly report and the inspection report go into one prompt; the response is constrained to the `WorkOrder` schema, validated, retried once with the validation error if needed, then checked by the safety floors.
- **Tool-calling chat:** automatic function calling over `lookup_sop`, `lookup_spare_part` and `get_machine_status`.
- **Resilience:** per-call timeout, two retries with exponential backoff on 429/5xx/transport errors, and a labelled fallback.

## Google Cloud services used
Vertex AI (Gemini) · Cloud Run (hosting, 2 vCPU / 2 GiB, min 1 instance) · Cloud Build and Artifact Registry (source deploy) · Cloud Storage (inspection images, exported work orders) · BigQuery (telemetry snapshots, anomaly flags, work orders, three analytics queries) · IAM service account with least-privilege roles. Designed but **not built:** Pub/Sub and Dataflow for streaming ingestion.

## Architecture summary
Streamlit UI on Cloud Run → telemetry generator and anomaly detector, vision inspector, and copilot → Gemini on Vertex AI. Work orders (PDF/JSON) and inspection images go to Cloud Storage; snapshots, anomaly flags and work orders are logged to BigQuery and queried back into the Fleet Analytics view. Production path (not implemented): PLC / OPC-UA → Pub/Sub → Dataflow → BigQuery. Diagram: `docs/architecture.md`.

## Challenges (real ones we hit)
- **Live testing found bugs that mocks hid:** a tool-calling crash caused by string type annotations, control characters in model text, a `0.0 h` remaining-life answer, and a transient proxy error that was not retried - each now fixed with a regression test.
- **Localization quality:** Gemini boxes were imprecise until we tested configurations and found that disabling extended thinking improved IoU from 0.25 to 0.45 and cut latency from about 11 s to about 6 s.
- **Streamlit state:** widget-bound keys vanish when a widget is not rendered, which silently reset per-machine fault settings; fixed by separating configuration from widgets.
- **Honest validation:** testing on a public real dataset exposed a load-sensitive baseline, and the live demo exposed a 43 % false-warning rate on healthy simulated assets that our mode-classification tests had missed. We fixed both with a deadband and report before and after.

## Honest limitations
- **Synthetic plant data.** Telemetry, SOPs, part numbers and cost figures are simulated or invented; the impact panel is driven by editable assumptions, not measurements.
- **Vision localization is approximate.** Measured live on `gemini-2.5-flash` against the known boxes of three synthetic samples (6 defects, 3 repeats): the correct defect type was found 13 of 18 times, mean box IoU was **0.45** on found defects (**0.32** counting misses as 0). Large features (weld crack, heat tint) scored about 0.5-0.6; small or subtle ones (gear wear patch, pitting clusters) scored about 0.0-0.4, and one wear patch was boxed in empty background. A bearing spall was labelled `other`. Small sample, synthetic images: indicative, not a benchmark.
- **Confidence values are the model's own,** self-reported and **not calibrated**.
- **Real-data detector check (CWRU bearing dataset, inner-race faults only):** the fault was detected in 12 of 12 streams; recall 99.96 % (1 missed window of 2,434), precision 99.0 %, F1 99.5 %, with 0 of 1,215 false alarms on normal data at the baseline's own motor load and 24 of 6,345 (0.4 %) when the load differed. **This is after an engineering-deadband fix; before it, precision was 74.1 % and cross-load false alarms were 849 of 6,345 (13.4 %)** - the fixed healthy baseline was load-sensitive. The deadband was chosen from simulator false alarms (43 % of healthy simulated windows raised a finding before; 0 of 300 after) and not iterated on CWRU, but CWRU was measured after the change, so the improvement is encouraging rather than independent confirmation. It covers one sensor, one rig and one fault type, rescaled accelerometer RMS rather than calibrated velocity, and says nothing about thermal-overload or cavitation detection on real machines. Subtler real faults may fall under the deadband.
- Streaming ingestion is a design, not an implementation. Not a certified safety system.

## What's next
Load-conditioned baselines to remove the remaining cross-load false alarms and a subtler-fault evaluation (the deadband may hide small real faults); a labelled defect benchmark (NEU / MVTec AD) to quantify localization and add box refinement or a dedicated detector for small features; calibrated confidence; real streaming ingestion (OPC-UA → Pub/Sub → Dataflow → BigQuery); Vertex AI Search over real maintenance documents instead of the illustrative SOP library; CMMS integration for work-order hand-off.
