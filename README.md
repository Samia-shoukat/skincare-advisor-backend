# Skincare Advisor — Backend

Backend service for an AI-assisted cosmetic skincare advisor, implemented
against **SRS-AISA-001 v1.3**.

The application photographs a user's face, identifies cosmetic concerns, and
returns an over-the-counter product routine. Where it sees something outside
what cosmetics can address, it stops and refers the user to a healthcare
professional instead.

**This is not a medical device and does not diagnose.** That constraint shapes
almost every design decision in this repository — see [Safety
constraints](#safety-constraints) before changing anything.

---

## Contents

- [Status](#status)
- [Quick start](#quick-start)
- [Safety constraints](#safety-constraints)
- [Architecture](#architecture)
- [Project layout](#project-layout)
- [Testing](#testing)
- [Database migrations](#database-migrations)
- [Known gaps](#known-gaps)

---

## Status

| Feature | Requirements | Backend | UI |
| --- | --- | --- | --- |
| 1. Onboarding | FR-ONB-001 … 008 | Complete, 43 tests passing | Not started |
| 2. Capture | FR-CAM-001 … 004 | Not started | Not started |
| 3. Triage & referral | FR-TRI-001 … 005 | Not started | Not started |
| 4. AI analysis | FR-AI-001 … 010 | Schema defined | — |
| 5. Recommendations | FR-REC-001 … 007 | Blocked (OI-002) | — |
| 6. Subscription & quota | FR-SUB-001 … 005 | Not started | — |

Feature 1 is verified at the API level. Several of its acceptance criteria are
**Demonstration** items in SRS Section 7 and can only be verified on the UI —
for example, that the date-of-birth screen does not state the minimum age. Those
remain open.

---

## Quick start

Requires Python 3.13 and a Supabase project.

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Create `.env` in the repository root:

```ini
ENVIRONMENT=development
LOG_LEVEL=INFO

# Supabase → Settings → Database → Connection string (URI)
DATABASE_URL=postgresql://postgres:PASSWORD@HOST:PORT/postgres

# Supabase → Settings → API → JWT Settings → JWT Secret
JWT_SECRET=your-project-jwt-secret

# Feature 3 onwards
GEMINI_API_KEY=
PROVIDER_RETENTION_DISABLED=false

QUOTA_ENFORCED=true
DEFAULT_SCAN_ALLOWANCE=1
CONSENT_STATEMENT_VERSION=1.2
```

`.env` is gitignored and must stay that way — it holds the database password.

Apply migrations and start the server:

```bash
alembic upgrade head
uvicorn app.main:app --reload
```

Interactive API docs: <http://127.0.0.1:8000/docs>
Readiness, with a per-requirement breakdown: <http://127.0.0.1:8000/readyz>

### Exercising the API before OAuth is wired up

The backend only *verifies* Supabase-issued tokens; it never issues one. Anything
holding the project secret can therefore mint a token it will accept, which is
useful during development:

```bash
python scripts/make_test_token.py
python scripts/make_test_token.py --subject second-user
```

Paste the result into the **Authorize** box in `/docs`. Development only.

---

## Safety constraints

Four constraints from the SRS override ordinary engineering preference. Each is
enforced structurally rather than by convention, because a convention is
something a future contributor can be unaware of.

### CON-001 — Zero-Save Policy

Face images are never written to disk, object storage, cache, log, or backup.
They exist in memory for the duration of one request.

- No model in `app/db/models/` has an image-shaped or binary column, and
  `tests/test_onboarding.py::test_no_table_has_an_image_shaped_column` fails the
  build if one appears.
- `PreparedImage.__repr__` is overridden so an exception traceback cannot render
  the payload.
- FR-CAM-004 also requires the analysis provider to have retention disabled.
  `/readyz` asserts this rather than assuming it.

### CON-002 — No diagnosis

The system never names a condition as a finding. Clinical signal identifiers
(`PIGMENT_PATCHES`, `SCALING_PLAQUES`, …) name an **appearance**, not a
condition, and are internal — the user sees only a plain-language observation.

Where condition names appear at all, they come from the Appendix H association
table under FR-AI-007, never from the model. The Appendix G response schema in
`app/schemas/analysis.py` has no field capable of carrying one, and rejects a
response that tries.

### CON-003 — No clinical judgement in application code

Rules, thresholds, and scoring live in `app/clinical/` as versioned JSON. Python
interprets them; it does not encode them.

If you find yourself writing `if concern == "ACNE"` in `app/engine/`, the logic
belongs in the matrix instead. The point of the split is that a reviewer who
does not read Python can still audit the clinical content.

### CON-004 — Single analysis interface

Every analysis task goes through `AnalysisProvider`. A task can move between an
internal model and a hosted provider by configuration alone, with no change to
triage, the rules engine, or the client.

---

## Architecture

```
Client  ──►  API (app/api)          request handling only
              │
              ▼
            Services (app/services) orchestration, fixed stage order
              │
              ├──►  Providers (app/providers)  vision analysis
              ├──►  Engine (app/engine)        deterministic rules
              └──►  DB (app/db)                persistence
                      │
                      ▼
                    Clinical content (app/clinical)   JSON, no code
```

### The scan pipeline order is fixed

SRS 4.2 specifies the order, and a stage that stops the flow prevents every
later stage. The ordering is not incidental:

```
quota check → referral flag → capture gate → image analysis
  → clinical signal check → rules engine → product matching → quota decrement
```

- FR-TRI-001 — the referral check precedes capture, so a flagged user never has
  a photograph taken at all.
- FR-SUB-002 — eligibility is evaluated before the provider is called, so an
  ineligible request never costs a provider call.
- FR-SUB-003 / FR-TRI-004 — the quota decrements last, and only when a routine
  is actually shown. Referral, unusable, and error outcomes leave it untouched:
  nobody should be charged for being told to see a doctor.

### Authentication

Sign-in happens client-side against Supabase using Google or Apple. This backend
verifies the resulting JWT signature and reads the subject claim. It never
issues a token, never verifies a password, and never stores credential material
(FR-ONB-001).

Password authentication was considered and rejected: it conflicts with
FR-ONB-001 and weakens the FR-ONB-003 age gate, since a new email address is far
easier to obtain than a new Google account.

---

## Project layout

```
app/
├── api/v1/routes/     endpoints — request handling, no business logic
├── clinical/          versioned JSON. No Python here by design (CON-003)
│   ├── matrix/        Appendix F ingredient rules
│   ├── associations/  Appendix H signal → condition table
│   ├── questionnaire/ FR-ONB-006 skin type instrument
│   └── copy/          IF-UI-001 single string resource
├── core/              enums, config, errors, logging, token verification
├── db/models/         SQLAlchemy models mapped to SRS 6.2
├── engine/            deterministic rules engine (FR-REC-001 … 004)
├── providers/         analysis backends behind one interface (CON-004)
├── schemas/           Pydantic request/response shapes
└── services/          orchestration, quota, content loading
```

`app/clinical/matrix/` and `app/clinical/associations/` deliberately contain no
`__init__.py`. They hold content, not code, and the absence of a `.py` file is
the visible form of CON-003.

---

## Testing

```bash
pytest
```

43 tests, ~2.5 seconds. Each names the requirement and acceptance criterion it
covers, so the suite doubles as the SRS Section 7 evidence trail.

Tests run against in-memory SQLite rather than Supabase — fast, isolated, and
runnable offline. `JSONType` in the models carries a SQLite variant to make this
work. The trade-off is that Postgres-specific behaviour (enums, JSONB indexing,
migrations) is not covered, so run the full flow through `/docs` against the real
database once per feature.

Two tests deserve attention if they ever fail:

- `test_under_thirteen_succeeds_and_blocks_scan_access` — asserts **200**, not
  403. Rejecting an under-13 date would state the threshold by implication and
  hand the user a second attempt at clearing it. FR-ONB-003 requires the minimum
  age never to be stated. If someone "fixes" this into a rejection, this test is
  what catches it.
- `test_no_table_has_an_image_shaped_column` — the structural half of CON-001.
  SRS Section 7 verifies this by inspection; an inspection performed once is
  worth less than an assertion that runs on every commit.

---

## Database migrations

```bash
alembic upgrade head                              # always before generating
alembic revision --autogenerate -m "description"
```

Read the generated file before applying it. Autogenerate infers intent from a
schema diff and is occasionally wrong — once tables exist, it will sometimes
propose dropping a column it cannot account for.

`alembic/env.py` takes the database URL from `app.core.config` rather than
`alembic.ini`, because `alembic.ini` is committed and the URL contains the
password.

### Supabase connection pooler

The pooler runs pgbouncer in transaction mode, which does not support prepared
statements. asyncpg caches them per connection by default, so the second request
reuses a statement name the new backend has never seen.

`PGBOUNCER_SAFE_CONNECT_ARGS` in `app/db/session.py` disables the cache, and
`alembic/env.py` imports the same constant. Defining it once matters: Alembic
builds its own engine, and a fix applied in only one place leaves migrations
failing while the application works.

---

## Known gaps

Carried from SRS Appendix D, plus findings from implementation.

### Blocking

| Item | Blocks | Note |
| --- | --- | --- |
| **Section 5 is missing** | FR-AI-010, FR-AI-007, FR-CAM-003 | The SRS jumps from 4.9 to 6. Sixteen NFR identifiers are referenced as dependencies but never defined. NFR-SAFE-008 supplies the model deployment thresholds — there are none, so the gate has nothing to compare against. |
| OI-002 | Feature 5 | The Appendix F rules matrix is unpopulated. `rules_matrix.v0.json` is a valid empty matrix so the pipeline runs end to end. |
| OI-012 | FR-AI-007 | The Appendix H association table is unpopulated. Every row ships disabled, which is also the correct default. |
| OI-014 … 018 | Internal models | No dataset register, and eight of the nine cosmetic concerns have no identified dataset. Every task currently routes to the hosted provider, and FR-AI-010 refuses to activate an internal model without a recorded evaluation. |
| DEP-003 | FR-ONB-008 | No practitioner engaged. The unsubstantiated state is implemented and is correct until one is. |

### Found during implementation

- **FR-ONB-002 contradicts FR-ONB-003.** FR-ONB-002 requires an age between 13
  and 80; FR-ONB-003 describes behaviour for a confirmed age of 12, which cannot
  exist if input rejects it. Resolved by accepting the date and withdrawing scan
  access silently — rejecting it would also teach the user what to enter next
  time. Needs an SRS amendment.
- **`SafetyAnswerChange` is a seventh entity.** SRS 6.1 lists six. It exists
  because safety answers are updatable by decision, and `referral_flag` is what
  FR-TRI-001 checks before any image is captured — an unrecorded change to it
  would leave no way to tell a correction from a bypass. Needs adding to 6.1
  and 6.2.
- **The FR-ONB-006 questionnaire is a draft.** OI-005 left the items undrafted.
  `skin_type.v0.json` adapts the sebum axis of the Baumann framework (REF-004,
  CC BY); every item and scoring rule carries `needs_review` and requires
  clinical sign-off under DEP-003 before baseline.
- **`date_of_birth` encryption is undecided.** SRS 6.2 marks it encrypted at
  rest. Supabase encrypts the volume, which covers a stolen disk; column-level
  encryption is a separate decision not yet taken.

---

## References

- `SRS-AISA-001 v1.3` — the governing specification
- REF-001 — AAD acne management guidelines (2024)
- REF-002 — NICE NG198, acne vulgaris management
- REF-004 — Baumann Skin Type Indicator (CC BY)