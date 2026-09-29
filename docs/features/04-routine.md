# Feature 4: Routine & Products

**Status:** Complete end to end in code. Needs the database migrated and the catalogue seeded before it runs against Supabase.
**Requirements:** FR-REC-001 … 007, FR-SUB-001, FR-SUB-005, DR-005 … 007

---

## Built

**Backend**
- `app/clinical/matrix/rules_matrix.v0.json`: Appendix F, the first draft (closes OI-002 pending review)
- `app/services/matrix_loader.py`: validation at load (DR-007), remote fetch with fallback (FR-REC-006)
- `app/engine/rules.py`: deterministic engine
- `app/engine/invariants.py`: the 7 FR-REC-004 checks, written independently of the engine
- `app/clinical/catalogue/catalogue.v0.json`: 23 products (closes OI-006 pending verification)
- `app/services/catalogue.py`: seeding plus the whole-bottle safety filter
- `app/services/routine_service.py`: engine → invariant check → products → save
- `app/db/models/catalogue.py`, `routine.py`, and migration `b7d2e4f81a93`
- `GET /v1/routines/latest`: FR-SUB-005
- `scripts/seed_catalogue.py`: a dry run unless `--apply` is passed

**Frontend**
- `screens/RoutineScreen.tsx`: AM/PM, budget / premium / generic, disclaimer pinned in the footer
- `lib/routineCache.ts` + `screens/OfflineRoutineFallback.tsx`: offline viewing (SRS 2.4)
- `ScanHomeScreen` shows the saved routine once the allowance is used (UC-007)

---

## Requirements

| ID | State |
| --- | --- |
| FR-REC-001 deterministic, priority | Met. Byte-identical output, no ML imported |
| FR-REC-002 minor exclusion | Met |
| FR-REC-003 pregnancy exclusion | Met |
| FR-REC-004 seven invariants | Met. 16,384 combinations, 0 violations; also re-checked live |
| FR-REC-005 tiered products | Met. Missing tier → generic text, recorded |
| FR-REC-006 external matrix | Met. URL + TTL + last-good fallback; version on every routine and scan log |
| FR-REC-007 persistent disclaimer | Met. Footer, outside the scroll view |
| FR-SUB-001 one full scan | Met |
| FR-SUB-005 access after allowance | Met. Includes offline |
| DR-005 no hard delete | Met. `ON DELETE RESTRICT`, tested with SQLite foreign keys on |
| DR-006 product completeness | Met. Validated at seed |
| DR-007 rule source + status | Met. Validated at load |

---

## Verified

- **202 backend tests** (up from 139)
- Mutation checks: disabling each catalogue safety condition makes a test fail
- Full migration chain applied on SQLite: no diff against the models, and it round-trips down and up
- Frontend `tsc --noEmit` clean in strict mode

**Not verified:** Postgres migration, the seed script against Supabase, the screens on a device, a real Gemini call.

---

## Bugs found on the way

- **Clear skin was treated as an unusable photo.** "No concerns returned" and "all concerns discarded" were handled the same way. Only the second is FR-AI-002's rule; clear skin now gets the base routine.
- **Offline viewing couldn't work from a cold start.** The profile check failed first, and the string bundle wasn't saved. Both fixed.

---

## To run it for real

```
alembic upgrade head
python scripts/seed_catalogue.py          # dry run
python scripts/seed_catalogue.py --apply
```

---

## SRS amendments needed

- **6.1:** Ingredient lives in the matrix, not the database (ADR-020)
- **6.1:** `routine_products` link table (DR-005 enforcement)
- **FR-REC-001:** one step per concern (ADR-021)

## Open

- **Clinical review of the matrix.** Most rules are `needs_review` (OI-001, OI-004).
- **Catalogue unverified.** Check local availability, formulation and price tier (DEP-004).
- No account-deletion screen in the app. The API exists, but there is no button (DR-002).
- `CLAIMS_VERSION` doubles as the consent version (from Feature 3).
