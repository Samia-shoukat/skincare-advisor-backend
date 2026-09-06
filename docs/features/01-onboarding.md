# Feature 1 — Onboarding

**Requirements** FR-ONB-001 … FR-ONB-008
**Status** Backend complete and verified · UI not started
**Tests** 43 passing

---

## Scope

- Account creation on first federated sign-in
- Date of birth capture, confirmation, and permanent lock
- Age band derivation and under-age scan restriction
- Four safety screening questions with referral flag
- Skin type determination from a questionnaire
- Limitations acknowledgement with version tracking
- Clinical review claim resolution
- Profile read and account deletion

---

## What was built

**Endpoints**

- `POST /v1/auth/session` — sign in, create account if new
- `POST /v1/me/date-of-birth` — confirm date, derive age band
- `POST /v1/me/safety-answers` — four answers, set referral flag
- `POST /v1/me/skin-type` — score questionnaire, store result
- `POST /v1/me/consent` — record acknowledgement with version
- `GET /v1/me` — onboarding state
- `DELETE /v1/me` — immediate hard delete
- `GET /v1/content/strings` — versioned claim bundle
- `GET /v1/content/questionnaire` — questions, weights stripped
- `GET /v1/content/review-claim` — review substantiation state

**Data**

- `users` — profile, flags, quota
- `safety_answer_changes` — audit of every safety answer submission
- `review_records` — signed clinical review, one per matrix version

**Clinical content**

- `app/clinical/questionnaire/skin_type.v0.json` — six items, scoring rules
- `app/clinical/copy/strings.py` — all user-facing claims

---

## Traceability

| Requirement | Implementation | Verification |
| --- | --- | --- |
| FR-ONB-001 | `create_session`, `auth_id` as primary key | 3 tests |
| FR-ONB-002 | `User.confirm_date_of_birth`, no PATCH/PUT route | 5 tests |
| FR-ONB-003 | `scan_access_blocked`, silent restriction | 3 tests |
| FR-ONB-004 | `AgeBand` derivation from confirmed date | 5 tests |
| FR-ONB-005 | `set_safety_answers`, `SafetyAnswerChange` | 6 tests |
| FR-ONB-006 | `services/questionnaire.py` + JSON config | 6 tests |
| FR-ONB-007 | `record_consent`, version comparison | 3 tests |
| FR-ONB-008 | `resolve_review_claim`, unsubstantiated default | 2 tests |
| IF-COMM-002 | `core/security.py`, `HTTPBearer` dependency | 3 tests |
| IF-COMM-003 | `core/errors.py`, two exception handlers | 1 test |
| IF-UI-001 | `clinical/copy/strings.py`, content endpoints | 2 tests |
| CON-001 | No image-shaped column in any model | 1 test |
| DR-002 | Cascade delete on account removal | 2 tests |

---

## Decisions taken

Full reasoning in `docs/architecture-decisions.md`.

- **ADR-001** Federated identity only. Password auth rejected — contradicts FR-ONB-001, weakens FR-ONB-003
- **ADR-002** Provider subject claim as primary key. Duplicate accounts structurally impossible
- **ADR-003** No PATCH/PUT for immutable fields. Guard on the model, not the route
- **ADR-004** Under-age dates accepted and restricted, not refused. Rejecting would state the threshold
- **ADR-005** Safety answers mutable, every submission audited
- **ADR-006** Clinical content as versioned JSON, no judgement in code
- **ADR-007** Questionnaire scored server-side, weights withheld from client
- **ADR-008** Immediate hard deletion
- **ADR-010** All user-facing copy served from the backend

---

## Verification

**Automated** — `pytest`, 43 tests, ~2.5s, in-memory SQLite

- Authentication and error shape — 4
- Account creation and identity — 3
- Date of birth and immutability — 5
- Under-age restriction — 3
- Age band derivation — 5
- Safety screening and audit — 6
- Skin type scoring and immutability — 6
- Consent versioning — 3
- Review claim default state — 2
- Full onboarding sequence — 2
- Deletion — 2
- Zero-Save structural check — 1

**Manual** — full flow through `/docs` against live Supabase, once, ending in `onboardingComplete: true`

**Two tests that must not be "fixed"**

- `test_under_thirteen_succeeds_and_blocks_scan_access` — asserts 200, not 403. A rejection would state the threshold by implication
- `test_no_table_has_an_image_shaped_column` — structural half of CON-001, runs on every commit rather than once by inspection

---

## Not covered

**Requires UI (Demonstration items, SRS Section 7)**

- FR-ONB-002 — that a confirmation screen presents the computed age
- FR-ONB-003 — that the date of birth screen does not state the minimum age
- FR-ONB-005 — that all four questions are presented
- FR-ONB-007 — that the limitations screen is presented before first scan

**Requires external input**

- FR-ONB-001 — Google and Apple OAuth not yet wired (DEP-002)
- FR-ONB-006 — questionnaire is a draft; every item marked `needs_review`, awaiting clinical sign-off (DEP-003)
- FR-ONB-008 — substantiated state untestable until a practitioner is engaged (DEP-003)

**Not covered by the test suite**

- Postgres enums, JSONB indexing, and migrations — tests run on SQLite

---

## Follow-up

- `SafetyAnswerChange` is a seventh entity; SRS 6.1 lists six. Needs adding to 6.1 and 6.2
- `date_of_birth` column-level encryption undecided. SRS 6.2 marks it encrypted at rest; Supabase encrypts the volume
- Retention of under-age accounts not decided
- Client-side fallback bundle for `/content/strings` on a cold start with no network
- Real support address in `SCAN_BLOCKED_SUPPORT` before release