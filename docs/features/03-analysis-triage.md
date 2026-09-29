# Feature 3 — Analysis & Triage

**Status:** Referral path complete end to end. A clear scan stops before the routine, because the rules engine is Feature 4.
**Requirements:** FR-AI-001 … 010, FR-TRI-001 … 005, FR-SUB-003

---

## Built

**Backend**
- `app/schemas/analysis.py`: Appendix G schema. Strict about structure, forgiving about vocabulary (unknown IDs are discarded and recorded). Output is deterministic.
- `app/services/analysis/`: one provider interface (CON-004), Gemini provider, and a task router with the FR-AI-010 deployment gate
- `app/services/association_table.py` + `associations.v0.json`: Appendix H, FR-AI-007/008
- `app/clinical/copy/observations.py`: curated observation text for every signal (closes OI-008)
- `app/clinical/copy/prohibited_terms.py`: the DR-008 list of condition names plus hedging phrases (closes OI-009)
- `app/services/scan_pipeline.py`: SRS 4.2 processing order
- `app/services/quota.py`: FR-SUB-003, idempotent
- `app/services/referral_summary.py`: FR-TRI-005
- `app/db/models/scan_log.py` + migration `a3f1c9d24e70`: the ScanLog table, with no image column
- `GET /v1/scans/referral`: the declared referral (UC-003), no photo involved

**Frontend**
- `screens/ScanHomeScreen.tsx`: eligibility-driven entry. A flagged user never sees the camera.
- `screens/ReferralScreen.tsx`: FR-TRI-003 content, shareable and selectable FR-TRI-005 summary
- `CaptureScreen`: routes on the server's error code; after three unusable photos, shows guidance with an exit (FR-AI-003)
- `uploadImage`: keeps the server's error code, sends an Idempotency-Key, no longer sends profile context

---

## Requirements

| ID | State |
| --- | --- |
| FR-AI-001 schema, reject invalid | Met. ANALYSIS_INVALID, quota untouched, ERROR row kept |
| FR-AI-002 closed concern list | Met. Discards recorded; all-discarded is treated as unusable |
| FR-AI-003 unusable → retake | Met. Guidance after 3 in a row |
| FR-AI-004 routing via config | Met. `ANALYSIS_PLAN`, `INTERNAL_MODEL_TASKS` |
| FR-AI-005 signals + observation | Met. DR-008 substitution, see ADR-018 |
| FR-AI-006 one-way | Met. The rules engine is never called when a signal is present |
| FR-AI-007 differential | Built, **shipped disabled** (ADR-016) |
| FR-AI-008 suppression + reason | Met. All four reasons are logged |
| FR-AI-009 model versions | Met. Tested with a stub internal model |
| FR-AI-010 deployment gate | Met. No internal provider registered (ADR-015) |
| FR-TRI-001 declared referral | Met. Refused before the body is read; `REFERRAL_REQUIRED` code |
| FR-TRI-002 signal referral | Met |
| FR-TRI-003 screen content | Met |
| FR-TRI-004 no quota on referral | Met |
| FR-TRI-005 summary | Met. See the SRS conflict below |
| FR-SUB-003 decrement conditions | Met, including idempotent retry |

---

## Verified

- **139 backend tests** (57 existing + 82 new), about 6s, SQLite, stub provider
- Migration applied on top of the existing two in SQLite: no diff against the models, and it round-trips down and up
- Frontend `tsc --noEmit` clean in strict mode

**Not verified:**
- A real Gemini call. The prompts are unchanged apart from the combined one.
- The migration against Postgres.
- The screens on a device.

---

## Bugs found on the way

- **Suite couldn't start.** `python-multipart` was missing, so `app.main` failed to import. `requirements.txt` was also UTF-16, so `pip install -r` would have failed on a fresh clone. Both fixed.
- **`gemini.py` imported a module that didn't exist.** Moved into `app/services/analysis/`.
- **Error-path audit rows were rolled back.** The session rolls back on exception, which discarded every UNUSABLE and ERROR log. Those outcomes now commit before raising.
- **An idempotent retry got QUOTA_EXCEEDED.** The scan being retried had used the last allowance. The replay lookup now runs before eligibility.
- **FR-AI-009 versions were never recorded.** The router copied nothing from the provider result.
- **"Solar lentigines" and "Hidradenitis suppurativa"** were in Appendix H but missing from the DR-008 list. A test now checks every association name against the list.

---

## SRS amendments needed

- **FR-TRI-005 contradicts itself.** It requires the declared safety answers *and* no condition names, but questions 2 and 4 name conditions. Answers are rendered with neutral labels (`SUMMARY_ANSWER_LABELS`).
- **ScanIneligibilityReason / REFERRAL_REQUIRED / NOT_REFERRED** are new error codes that 4.2 does not list.
- **ADR-015 and ADR-016** narrow v1.3 scope; Appendix I and Appendix H should say so.

## Open

- **Feature 4:** rules engine, Appendix F matrix, catalogue, Routine entity, FR-REC-*, FR-SUB-005
- `CLAIMS_VERSION` doubles as the consent version the client acknowledges. The two should be separated before any claim string changes.
- The capture gate prompts are still hard-coded in `CaptureScreen` (existing IF-UI-001 gap).
- `/readyz` still reports `provider_retention_disabled`. That must be true before any real scan.
