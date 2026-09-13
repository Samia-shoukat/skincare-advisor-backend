# Feature 1 — Onboarding

**Done.** Backend + UI + tests + docs.

---

## Built

**10 endpoints** — session, date of birth, safety answers, skin type, consent, profile, delete, plus three content endpoints.

**8 screens** — welcome, register, login, forgot password, date of birth (with confirmation), safety questions, skin type, consent, restricted, completion.

**3 tables** — `users`, `safety_answer_changes`, `review_records`.

**Auth** — Google and email+password via Supabase. Backend never sees a password.

---

## Requirements

| ID | Covered by |
| --- | --- |
| FR-ONB-001 | Sign-in, account creation, no duplicates |
| FR-ONB-002 | DOB confirmed once, then locked |
| FR-ONB-003 | Under-age: account kept, scan silently blocked |
| FR-ONB-004 | Age band derived server-side |
| FR-ONB-005 | Four safety questions, changes audited |
| FR-ONB-006 | Skin type scored server-side from JSON config |
| FR-ONB-007 | Consent recorded with version |
| FR-ONB-008 | No review claim without a signed record |

Plus IF-COMM-002/003, IF-UI-001, CON-001, DR-002.

---

## Four decisions worth remembering

**Under-age returns 200, not an error.** An error message tells the user what to type next time.

**No route exists to change DOB or skin type.** Guard sits on the model, not the route.

**Clinical content is JSON, not code.** So a reviewer can audit it without reading Python.

**All user-facing copy comes from the server.** No claim is written into a screen.

---

## Verified three ways

- 43 unit tests, ~2.5s, SQLite
- 34 Postman requests, ~70 assertions, live database
- Full UI walkthrough in browser

Two things checked by hand and **not** protected by tests: DOB screen states no minimum age; restricted screen shows only a support address.

---

## Three bugs found

**Supabase signs tokens with ES256, not a shared secret.** Every real sign-in failed while all tests passed — because the tests minted their own HS256 tokens.

**Concurrent sign-in created two accounts.** Client called session twice. Fixed on both sides.

**Browser couldn't reach the API.** No CORS. Never surfaced because Postman and pytest aren't browsers.

---

## Open

**Blocked on people, not code**

- Skin type questionnaire is a draft — needs clinical sign-off
- No reviewer engaged, so the app claims no clinical review
- Apple sign-in wired but not configured

**Needs SRS amendment**

- FR-ONB-001 forbids handling passwords — we now do (via Supabase)
- FR-ONB-002 and 003 contradict each other on under-age dates
- `SafetyAnswerChange` is a seventh entity; SRS lists six

**Small**

- DOB column-level encryption undecided
- Real support email still a placeholder
- Under-age account retention undecided

---
