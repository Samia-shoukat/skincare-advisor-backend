
---

## `README.md` 

```markdown
# Skincare Advisor

A skincare app that looks at a photo of your face, works out what it can help
with, and builds a routine from over-the-counter products.

The part that matters more: when it sees something cosmetics can't help — a
wound, a mole that's changed, signs of a skin condition — it stops, says what it
saw, and tells you to see a doctor. It does not guess at a diagnosis.

Built against a written specification (`SRS-AISA-001 v1.3`). Requirement-level
traceability lives in `docs/features/`; this file is about running and
understanding the thing.

---

## What works today

**Onboarding is finished and running.** A user can sign in with Google or with
an email and password, give their date of birth, answer four safety questions,
take a short questionnaire that determines their skin type, and read what the
app can and cannot do before agreeing to use it. All of that is live against a
real database, tested three ways, and walked through end to end in the UI.

**Nothing else is built yet.** The camera, the analysis, the routine engine and
the scan quota are all still to come. Two of them are blocked on clinical
content that hasn't been written — see [What's missing](#whats-missing).

| Feature | State |
| --- | --- |
| Onboarding | Done — backend and UI |
| Camera capture | Not started |
| Referral and triage | Not started |
| Image analysis | Response format defined, nothing wired up |
| Routine recommendations | Blocked — the ingredient rules don't exist yet |
| Scan quota | Not started |

---

## Running it

Two repositories. The backend is Python, the app is React Native.

### Backend

Needs Python 3.13 and a Supabase project.

```bash
cd skincare-advisor-backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

Create `.env` in that folder:

```ini
ENVIRONMENT=development
LOG_LEVEL=INFO

DATABASE_URL=postgresql://postgres:PASSWORD@HOST:PORT/postgres
SUPABASE_URL=https://yourproject.supabase.co
JWT_SECRET=your-project-jwt-secret

GEMINI_API_KEY=
PROVIDER_RETENTION_DISABLED=false

QUOTA_ENFORCED=true
DEFAULT_SCAN_ALLOWANCE=1
CONSENT_STATEMENT_VERSION=1.2
```

All four Supabase values come from the dashboard: the connection string from
Database settings, the rest from API settings. `.env` is gitignored and must
stay that way.

```bash
alembic upgrade head
uvicorn app.main:app --reload --host 0.0.0.0
```

`--host 0.0.0.0` matters for anything except a browser on the same machine —
an emulator or a phone can't reach a server listening only on localhost.

API docs: <http://127.0.0.1:8000/docs>
Health, with a breakdown of what's configured: <http://127.0.0.1:8000/readyz>

### App

```bash
cd skincare-advisor-frontend
npm install
npx expo start
```

Press `w` for a browser, `a` for an Android emulator. Its `.env` needs three
values:

```ini
EXPO_PUBLIC_SUPABASE_URL=https://yourproject.supabase.co
EXPO_PUBLIC_SUPABASE_ANON_KEY=your-anon-key
EXPO_PUBLIC_API_URL=http://127.0.0.1:8000
```

That last one changes depending on where you're running it, and getting it
wrong is by far the most common cause of "can't reach your profile":

| Running on | API URL |
| --- | --- |
| Browser | `http://127.0.0.1:8000` |
| Android emulator | `http://10.0.2.2:8000` |
| Physical phone | your machine's LAN address, e.g. `http://192.168.1.5:8000` |

`.env` is only read when Expo starts. Change it and you have to restart, not
reload.

### Testing without waiting for emails

Supabase's free tier sends two or three emails an hour, which is not enough to
test a sign-up flow. The backend only *checks* tokens — it never issues one — so
anything holding the project secret can produce a token it will accept:

```bash
python scripts/make_test_token.py --subject test-user-1
```

Paste it into the **Authorize** box in `/docs`, or into the developer sign-in
screen in the app. Development only, and it never leaves your machine.

---

## Rules the code doesn't break

Four of these. They're not style preferences, and each one is enforced by the
structure of the code rather than by everyone remembering.

### Face photos are never stored

Not to disk, not to a cache, not to a log, not to a backup. A photo exists in
memory for the length of one request and then it's gone.

There is no column anywhere in the database capable of holding an image, and a
test fails the build if one appears. The object that carries image bytes
overrides how it prints itself, so an unexpected crash can't dump a photo into
a log file. And the app refuses to report itself healthy unless the vision
provider is configured with data retention switched off — a promise about
storage means nothing if the third party keeps a copy.

### The app never names a condition as a finding

When the analysis sees something outside cosmetic range, it reports what it
*looks like*, not what it *is*: "areas visibly darker than the surrounding
skin", not "melasma". The internal identifiers describe appearance and never
reach the screen.

Condition names, where they appear at all, come from a reviewed lookup table
and only ever as a list of possibilities with fixed wording around them. The
format the analysis returns has no field capable of carrying a diagnosis, and
rejects a response that tries to add one.

### Clinical decisions live in JSON, not Python

Which ingredient suits which skin type, which ones a pregnant user shouldn't
use, how the skin type questionnaire is scored — all of it sits in
`app/clinical/` as versioned files. The Python reads those files; it doesn't
contain the decisions.

The point is that a dermatologist reviewing this content shouldn't have to read
code. Those folders deliberately contain no `.py` files at all.

If you catch yourself writing `if concern == "ACNE"` in the rules engine, the
logic belongs in the JSON instead.

### One way in and out for image analysis

Every analysis task goes through a single interface. Moving a task between our
own model and a hosted provider is a configuration change — nothing in the
triage logic, the rules engine, or the app has to know which one answered.

---

## How it's put together

```
App  ──►  API layer          takes requests, returns responses, no logic
           │
           ▼
         Services            orchestration, and the fixed order below
           │
           ├──►  Providers   image analysis
           ├──►  Engine      the deterministic rules
           └──►  Database
                   │
                   ▼
                 Clinical content    JSON, reviewed separately
```

### The scan runs in a fixed order

```
quota check → referral flag → capture gate → image analysis
  → clinical signal check → rules engine → product matching → quota decrement
```

The order isn't arbitrary. The referral check comes before capture, so someone
who told us about an open wound never has a photo taken at all. Eligibility is
checked before the provider is called, so a request that can't succeed doesn't
cost anything. And the quota decrements last, only when a routine is actually
shown — nobody should be charged for being told to see a doctor.

### Sign-in

Handled by Supabase. Google, Apple, or an email and password. Supabase issues a
signed token; this backend checks the signature and reads who it belongs to. It
never sees a password, and there is no code here that could store one.

Supabase signs those tokens with a private key and publishes the matching
public key, which the backend fetches once and caches. It also still accepts
the older shared-secret format, and that's deliberate rather than left over:
it's what makes the local test tokens above possible.

---

## Layout

```
skincare-advisor-backend/
├── app/
│   ├── api/v1/routes/     endpoints
│   ├── clinical/          JSON content — no Python here by design
│   ├── core/              config, errors, logging, token checking
│   ├── db/models/         database tables
│   ├── engine/            rules engine
│   ├── providers/         image analysis backends
│   ├── schemas/           request and response shapes
│   └── services/          orchestration
├── docs/                  decisions and feature records
├── scripts/
└── tests/

skincare-advisor-frontend/
├── components/            shared UI pieces
├── lib/                   API client, auth state, design tokens
└── screens/
    └── onboarding/
```

---

## Testing

```bash
pytest
```

43 tests, about two and a half seconds. Each one names what it's checking and
why, so the suite doubles as evidence of what's been verified.

They run against an in-memory database rather than Supabase — fast, isolated,
and they work offline. The trade-off is that Postgres-specific behaviour and
migrations aren't covered, so the full flow gets walked through against the real
database once per feature.

There's also a Postman collection in `docs/postman/` with 34 requests. It mints
its own tokens and cleans up after itself, so it can be run repeatedly. Import
it, set `baseUrl`, `jwtSecret` and `consentVersion` in an environment, and hit
Run. **Don't commit the exported environment** — it holds the secret.

Two tests are worth knowing about before you touch them:

**The under-age test expects a success, not an error.** When someone enters a
date of birth below the minimum, the request succeeds, the account is created,
and scanning is quietly switched off. It looks wrong. It isn't: an error message
tells the user exactly what to type on their second attempt, which defeats the
entire point of the check. If someone "fixes" this into a rejection, that test
is what catches it.

**The no-image-column test** walks every table looking for anything that could
hold a photo. It's the structural half of the storage promise, and it runs on
every commit rather than being checked by hand once.

---

## Database

```bash
alembic upgrade head                              # always do this first
alembic revision --autogenerate -m "description"
```

Read the generated migration before applying it. Autogenerate works out what
changed by comparing your models to the live schema, and it occasionally gets
that wrong — once tables exist it will sometimes propose dropping a column it
can't account for.

The database URL comes from `.env`, not from `alembic.ini`, because
`alembic.ini` is committed and the URL contains the password.

### One gotcha worth knowing

Supabase's connection pooler doesn't support prepared statements, and the
Postgres driver uses them by default. The result is a confusing error on the
*second* request, not the first.

The fix is one setting, defined once in `app/db/session.py` and imported by
Alembic. Defining it in only one place is the point — Alembic builds its own
connection, and fixing just one of them leaves migrations failing while the app
works fine, which is a genuinely annoying thing to debug.

---

## What's missing

Honest list. Some of these are outside the code.

**The ingredient rules don't exist.** The routine engine is written and tested,
but the actual clinical content — which ingredient for which skin type, at what
strength, which combinations to avoid — hasn't been compiled yet. The engine
currently loads an empty ruleset and reports every step as omitted, which is
correct behaviour for empty content but obviously not a product.

**The condition lookup table is empty**, and every row ships disabled. That's
also the right default: naming conditions changes what this app legally is, and
nothing gets enabled without a reviewed source and a measured accuracy figure.

**No clinical reviewer yet.** The app therefore makes no claim to be clinically
reviewed, which is enforced in code — the claim only appears if a signed review
record exists for the current ruleset.

**The skin type questionnaire is a draft.** Six questions adapted from a
published framework, with every item and every scoring threshold marked as
needing review. It works and it's deterministic; it hasn't been validated.

**No training data for the image analysis.** Eight of the nine cosmetic
concerns have no identified dataset. Everything currently routes to a hosted
provider, and the code refuses to activate an internal model that has no
recorded evaluation.

**The specification has a missing section.** The non-functional requirements —
performance targets, accuracy thresholds, security requirements — are referenced
throughout but were never written. Four of them block real work, most
importantly the accuracy thresholds a model would have to meet before it could
be used at all.

**Two smaller things.** Whether the date of birth needs encrypting at the column
level is undecided. And leaked-password checking is a paid Supabase feature,
currently off.

---

## Reading further

- `docs/architecture-decisions.md` — every significant decision, why it was
  made, and what it cost
- `docs/features/01-onboarding.md` — what was built, what was verified, what
  wasn't
- `docs/postman/` — the API test collection
```

---

