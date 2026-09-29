# Feature 5: Account, Privacy & Store Readiness

**Status:** Built and tested in code; needs settings filled in and a device test.
**Covers:** DR-002, DR-003, DR-004, FR-ONB-003, FR-ONB-007, Google Play account-deletion and privacy-policy requirements

---

## Built

**Backend**
- `DELETE /v1/me` now deletes every row **and the Supabase sign-in**. If removing the sign-in fails, nothing is deleted (`ACCOUNT_DELETION_FAILED`).
- `app/services/account_deletion.py`: explicit, ordered erase. It doesn't depend on SQLite enforcing foreign keys.
- `app/services/retention.py` + `scripts/retention.py`: DR-003 / DR-004. A dry run unless `--apply` is passed.
- `/legal/privacy`, `/legal/terms`, `/legal/account-deletion`: public HTML pages, suitable for the Play Store listing
- `SUPPORT_EMAIL`, `OPERATOR_NAME` and `SUPABASE_SERVICE_ROLE_KEY` are now settings
- `/readyz` fails in production if the support email is still the placeholder or the service key is missing
- The consent version is now separate from the strings version (the `consentVersion` field)

**Frontend**
- `screens/AccountScreen.tsx`: privacy, terms, support email, sign out, and delete with two-step confirmation
- `lib/useBackHandler.ts`: Android back on every screen (capture, account, referral, DOB confirm, auth). It uses the built-in `BackHandler`, so no dev-client rebuild is needed.
- Privacy and terms links on the Welcome and Consent screens
- Sign-out and deletion clear the offline routine copy from the phone

---

## Verified

- **213 backend tests** (up from 202). The deletion test was mutation-checked: removing the explicit deletes makes it fail.
- Frontend `tsc` is clean
- **Not verified:** a real Supabase admin deletion, and the screens on a device

---

## You must set (backend `.env`)

| Setting | Where from |
| --- | --- |
| `SUPPORT_EMAIL` | A real inbox someone reads |
| `OPERATOR_NAME` | Your name, team or university project name |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase → Project Settings → API → `service_role`. **Server only. Never put it in the frontend or commit it.** |
| `PROVIDER_RETENTION_DISABLED` | `true` **only** if Gemini is on a paid plan |

---

## Decisions

- **Deletion is all or nothing.** Data first (not committed), then the sign-in, then commit. Never half-deleted.
- **Retention keeps scan logs that have a routine,** so an active user keeps their routine (FR-SUB-005).
- **DR-004's notification is manual.** There is no email channel. The script lists who is due; notify them, then run it with `--accounts`.
- **Legal pages are drafts written from the code's actual behaviour.** They need review before publishing. They show a "do not publish" banner while `PROVIDER_RETENTION_DISABLED` is false.
- **No navigation library.** `BackHandler` covers Android back without a native rebuild. Revisit when screens multiply.

## Open

- Schedule `scripts/retention.py` (daily cron on the host)
- Email channel for DR-004 notifications
- Legal review of the privacy policy and terms
- Rate limiting / email verification against multi-account abuse
