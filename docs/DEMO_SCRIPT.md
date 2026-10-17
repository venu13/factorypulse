# FactoryPulse - 3-minute demo script

**Live app:** https://factorypulse-gnbcujjj5q-uc.a.run.app · **Target length:** 3:00 edited · **Built around:** the weld image

Everything below was exercised against the deployed app (headless run: demo button, tabs, triage, chat buttons, validation panel). Button and tab names are exactly as they appear on screen.

## Before you record (2 minutes)
1. Open the URL in a **fresh private window**, 100 % zoom, about 1500 × 1000. A new session resets the fleet: PUMP-101 / PUMP-301 / FAN-401 **HEALTHY**, PUMP-102 **WARNING**, MTR-201 / MTR-202 **CRITICAL**.
2. Confirm the sidebar badge says **VERTEX AI** (not "OFFLINE MOCK") and shows `gemini-2.5-flash · project-6856615b-d7ea-4a2b-85d / us-central1`.
3. Do one **warm-up run** of the Demo Scenario to confirm the Gemini path is healthy before you record, then close the window and open a fresh private one for the real take (a new session resets the fleet to its starting state).
4. Do not press "Re-roll sensor noise". Keep the Diagnostics expander closed.
5. **Abort rule:** if any badge says `fallback (...)` or `mock`, stop - do not record it. Open the "Error details" expander, check `gcloud run services logs read`, and retry.

## Measured Gemini waits (so you know what to cut)
| Step | Observed live | Edit |
|---|---|---|
| Demo step 3 - Gemini vision on the weld | 4 – 8 s (1-2 defects found; varies by run) | cut to the result |
| Demo step 4 - Gemini work order | 12 – 35 s (slowest call) | cut to the result |
| Whole Demo Scenario | 17 – 40 s | show ~10 s of it |
| Page finishing its re-render after "Demo scenario complete" (cards, charts) | ~10 s | cut; wait for "RUNNING…" to disappear before pointing at the fleet cards |
| Copilot chat reply | 1.5 – 11 s | cut to the reply |
| Fleet analytics (BigQuery queries) | ~5 – 10 s after the page loads | cut to the charts |

Cut markers below look like **✂️ CUT**. Show the "⏳ Calling Gemini…" line for about 2 seconds so viewers see it is a live call, then cut to the ✅ line.

## Script

| Time (edited) | On screen - exact clicks | Say |
|---|---|---|
| **0:00 - 0:15** | **System Overview** tab, nothing clicked. Point at the sidebar **VERTEX AI** badge, then the six fleet cards. | "Unplanned downtime and missed cracks cost plants millions because the evidence - sensor drift, a photo, the SOP binder - lives in three places, so triage is slow and nobody can audit it. FactoryPulse puts them together with Gemini on Vertex AI, running here on Cloud Run." |
| **0:15 - 0:25** | Hover the PUMP-101 card (**HEALTHY**, health 100/100), then MTR-202 (**CRITICAL**, 45/100). KPIs read **3 healthy · 1 warning · 2 critical**, fleet health 77/100. | "Six simulated assets. PUMP-101 is healthy and stays quiet; three others are already flagged - one warning, two critical." |
| **0:25 - 0:30** | Click **▶ Run demo scenario**. | "One click runs the whole path." |
| **0:30 - 1:05** | The status box shows steps. **Step 1** ✅ (fault injected on PUMP-101) - **Step 2** ✅ (detector: CRITICAL, inner-race bearing defect, vibration over the critical limit). **Step 3** shows "⏳ **3.** Calling **Gemini vision**…" - hold ~2 s. **✂️ CUT (4-8 s)** → ✅ "Weld inspection: FAIL, 2 defect(s) … [gemini-live (gemini-2.5-flash)]". **Step 4** shows "⏳ **4.** Calling **Gemini** to build the work order…" - hold ~2 s. **✂️ CUT (12-35 s)** → ✅ "Work order INC-PUMP-101-… CRITICAL, RUL …, LOTO required … [gemini-live …]". **Step 5** ✅ "PDF export ready". Panel title turns **Demo scenario complete**. | Over steps 1-2: "I inject a bearing fault; the detector flags rising vibration and attributes it to an inner-race defect." Over 3-4: "Gemini inspects a weld photo, then correlates telemetry, the image and our SOPs into a work order." After: "Every result carries a provenance badge - this one is live Gemini, not a mock or a fallback." Then, once "RUNNING…" has gone (~10 s, cut it), point at the **PUMP-101** card: now **CRITICAL**, health 40/100, KPIs **2 healthy · 1 warning · 3 critical**, fleet health 67/100. |
| **1:05 - 1:30** | In **Last demo scenario result**, click **📄 Download PDF**, open the file: header shows *Provenance: gemini-live (gemini-2.5-flash)*, criticality badge, LOTO required, parts, numbered steps. Switch back to the app. | "The work order exports as a PDF with the provenance and the lock-out/tag-out steps first. Parts and procedures come from the SOP library - which is synthetic here, labelled as such in the footer." |
| **1:30 - 1:50** | Click the **🔍 Visual QA Lab** tab (PUMP-101 and the weld sample are already selected by the demo). Show the **Gemini annotated** image: the branching crack boxed in red (**CRITICAL 95 %**) and the heat-tint patch boxed in amber (**MEDIUM 80 %**); cards read **FAIL · 20/100 · 2 defects** (some runs return only the crack - that is fine, say "Gemini found the crack"). Point at the provenance badge and the grey note under the image. | **Caveat (one sentence):** "Box localization is approximate - mean IoU 0.45 on defects found, good on large features like this crack and weak on small ones - and the confidence is the model's own number, not calibrated." |
| **1:50 - 2:15** | Click **🛠️ Field Engineer Copilot**. Show the work-order card: **CRITICAL**, **LOTO REQUIRED**, remaining useful life (**0.5 h**, or "0.0 h (immediate - failure imminent)" with a 🛡️ note), root cause, and the `gs://…` "Saved to Cloud Storage" line. In the chat box (**Ask the copilot...**) type exactly: `Use the lookup_sop tool for the bearing_inner_race failure mode and tell me its SOP document number.` and press Enter. **✂️ CUT (1.5-11 s)** → reply names **SOP-MECH-021**, badge **● gemini-live (gemini-2.5-flash) · … ms · 1 tool call(s)**. | "Triage output is schema-validated, then checked by deterministic safety floors - a critical fault can never come out as low priority, and any override is shown. The chat answers by calling a SOP lookup tool - that badge is the proof. The PDF and JSON were saved to Cloud Storage." |
| **2:15 - 2:35** | Click **📊 System Overview**, scroll to **Fleet analytics**. **✂️ CUT (5-10 s)** until the three charts render. Point at the line "Data source: **BigQuery (project-6856615b-d7ea-4a2b-85d.factorypulse)**". | "Telemetry snapshots, anomaly flags and work orders are logged to BigQuery, and these three queries - anomalies per machine per day, time to detect, work orders by criticality and provenance - run live against it." |
| **2:35 - 2:50** | Click **📈 Telemetry Anomaly Explorer**, scroll to the bottom, click **🧪 Real Data Validation - CWRU bearing dataset**. Show Precision 99.0 %, Recall 99.96 %, FP rate 0.3 %, and the grey "Before the engineering-deadband fix" line (precision 74.1 %). | **Caveat (one sentence):** "On the public CWRU bearing data it caught 12 of 12 faults at 99 % precision, but that is one rig and one fault type, it was 74 % precision before we fixed a load-sensitive baseline, and it says nothing yet about thermal or cavitation faults." |
| **2:50 - 3:00** | Back to **System Overview**, top of page. Optional: show `docs/architecture.md` diagram for 3 s. | "Here the telemetry is simulated. In production the same copilot sits on a real pipeline: an **OPC-UA** gateway into **Pub/Sub**, through **Dataflow**, into **BigQuery**. FactoryPulse - from sensor drift to a safe, auditable work order." *(end card: the URL)* |

## Timing check
Sum of edited segments: 15 + 10 + 5 + 35 + 25 + 20 + 25 + 20 + 15 + 10 = **180 s**. Raw (uncut) recording will run about **4:30 - 5:30** because of the Gemini and BigQuery waits.

## Chat tool calls are the model's decision
Verified on the deployed app: the explicit `lookup_sop` question above and "Look up the spare part for the bearing and give me its part number." both produced a **1 tool call(s)** badge, while the quick button **Which parts do I need?** and "Which SOP applies to this failure, and what is its document number?" answered **without** a tool call. Use the explicit wording. If the badge shows no tool call, ask the second phrasing once; do not claim a tool call the badge does not show.

## If something goes wrong on camera
- **A badge says `fallback (...)`:** do not use the take. The exact error is in the *Error details* expander; the app already retried twice. Re-run; if it persists check Cloud Run logs and the service-account roles.
- **The Demo Scenario shows no progress for > 60 s:** in testing one run out of ~6 never completed within 4 minutes (not reproduced, no errors in the service logs, other visitors were on the app at the time). Reload (a new session resets everything) and run it again; do not wait it out.
- **Gemini is slow (> 60 s):** that is the call timeout plus a retry; cut it in editing or re-run.
- **The Overview looks dimmed with "RUNNING…" in the corner after the demo:** the page is still computing the BigQuery charts below the cards; wait 5-10 s before pointing at them.
- **A healthy asset shows WARNING:** this was a real bug (43 % of healthy simulated windows raised a finding) fixed with an engineering deadband; the deployed revision `factorypulse-00002-…` includes the fix. If you see it, the wrong revision is serving.

## Honest-framing checklist (say or show each at least once)
- [ ] Provenance badge is **gemini-live** - shown at the demo result.
- [ ] Plant data, SOPs and part numbers are **synthetic** - footer visible, mentioned at the PDF.
- [ ] Localization is **approximate**, confidence is **uncalibrated** - QA Lab sentence.
- [ ] CWRU result is **one rig, inner race only**, with the before/after - validation sentence.
- [ ] The chat reply badge shows **1 tool call(s)** (or you do not claim tool use).
- [ ] Streaming ingestion is **designed, not built** - closing line says "in production".
