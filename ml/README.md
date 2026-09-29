# Model training — acne detection

Trains the one analysis task the SRS says a custom model may serve
(Appendix I: *acne detection, acne04v2 provides face-format labels*), measures
it, and produces the Appendix I register entry.

**Training does not deploy anything.** FR-AI-010 is explicit: a task is served
by a trained model *only* where a recorded evaluation meets the thresholds. The
notebook prints a pass/fail verdict; the model reaches users only if it passes
and you then switch it on in configuration.

---

## What you do, in order

### 1. Get the dataset (yours to accept)
**ACNE04** — from *Joint Acne Image Grading and Counting via Label Distribution
Learning* (Wu et al., ICCV 2019). 1,457 facial photographs with Hayashi
severity grades.

- Find the authors' repository and follow **their** download instructions.
- **Read the licence and confirm it permits your use** (academic project work).
  This is CON-007: only datasets whose licence permits the intended use.
- Record the licence text and source URL — Appendix I requires both.

### 2. Put it in Google Drive
Upload the extracted dataset to Drive, e.g. `MyDrive/skinsight/acne04/`. The
notebook mounts Drive and reads from there, so a disconnected Colab session
doesn't lose it.

### 3. Open the notebook in Colab
`notebooks/acne_training.ipynb` → open in Google Colab.
**Runtime → Change runtime type → T4 GPU.** Free tier is plenty for 1,457 images.

### 4. Set one value, then Run all
In the **Config** cell set `DATA_ROOT` to your Drive folder. Then
**Runtime → Run all**. Expect 15–30 minutes.

### 5. Send me the output
The last cell prints a JSON block. Paste it back to me and I will:
- write it into `ml/appendix_i.json` (the SRS Appendix I register),
- wire the model in behind the FR-AI-010 gate if it passed,
- record the decision as an ADR either way.

---

## What the notebook does

**Option A — our model.** Fine-tunes `google/vit-base-patch16-224-in21k`
(Apache-2.0, Google-documented provenance) on ACNE04. A Hugging Face backbone
whose origin is documented is what lets the entry be completed in Appendix I.

**Option B — the comparison.** Runs `imfarzanansari/skintelligent-acne` (MIT)
over the *same* held-out images and prints a head-to-head table, overall and
per skin tone. It runs locally, so no dataset image leaves the machine -- which
also sidesteps the licence question that rules out a Gemini comparison.

Read the table fairly in your report: ACNE04 is **in-domain** for our model and
**out-of-domain** for the baseline, so a win is expected and is not by itself
evidence of a better model. The notebook prints that caveat with the table.

The baseline uses its own six-level scale, folded onto our three. The exact
mapping is printed for your appendix, and any label that cannot be mapped is
scored as wrong rather than quietly dropped.

## What the notebook measures

| Metric | Why |
|---|---|
| Accuracy, macro-F1, per-class recall | Overall quality across severity grades |
| Sensitivity / specificity for "acne present" | This is what the routine actually branches on |
| Confusion matrix | Shows *how* it is wrong, not just how often |
| **Per skin-tone group** | NFR-SAFE-008 demands this. A model that works on light skin and fails on dark skin is not an accurate model |
| Inference time on CPU | NFR-PERF-001, and SRS 2.4 says CPU + ONNX Runtime in production |

### About the skin-tone breakdown
ACNE04 carries **no skin-tone labels**, so the notebook estimates them with
**ITA** (Individual Typology Angle) from the image itself — a standard method
that buckets skin into very light → dark from CIELAB values.

It is an estimate, and the notebook says so in its output. It is also the only
way to produce the breakdown NFR-SAFE-008 requires from a dataset that lacks
the labels (OI-016). Treat the groups as approximate, and say so in your report.

---

## The deployment gate

Proposed thresholds, in the notebook's `THRESHOLDS` cell:

| Check | Proposed value |
|---|---|
| Macro-F1 across grades | ≥ 0.55 |
| Worst skin-tone group accuracy | ≥ 0.75 |
| Max gap between tone groups | ≤ 0.15 |
| Beats the public baseline | macro-F1 higher |
| ONNX output matches PyTorch | exact |

Specificity is **not** in the gate because ACNE04 cannot measure it: every
image has acne, so "how often does it invent acne on clear skin" is unanswerable
from this dataset. The notebook says so rather than omitting it.

⚠️ **These are my proposal, not your SRS.** Section 5 is missing from your
document, so NFR-SAFE-008 has no defined numbers — while FR-AI-010 gates
deployment on them. Agree the numbers with your supervisor and write §5.

---

## If it fails the gate

That is a result, not a failure of the project. Your SRS already anticipates it:

> **ASM-005** — *A model trained on acne04v2, whose population differs from the
> target population, transfers at acceptable accuracy.* If false: *"Acne
> detection reverts to the hosted provider. The trained model is retained as an
> evaluation artefact only."*

"We trained it, measured it honestly, it did not clear the bar, so we did not
ship it" is a stronger finding than a number with no evaluation behind it — and
the routing code already supports exactly that outcome (ADR-015).

---

## One thing to check before comparing against Gemini

It is tempting to run the same test images through Gemini for a head-to-head.
**Check the dataset licence first.** Sending licensed research images to a third
party may breach it, and it would also put faces you do not own through a
commercial API. If the licence does not clearly allow it, don't — compare
against the published ACNE04 baselines instead.
