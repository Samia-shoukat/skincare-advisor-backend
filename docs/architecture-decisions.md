### ADR-012 — Email and password authentication
**FR-ONB-001, FR-ONB-003** · Accepted
**Supersedes in part ADR-001 and ADR-011**

Sign-in offers Google or email with a password. Choosing email opens a sheet:
create an account, or log in.

ADR-011 chose a one-time code over a password on the grounds that FR-ONB-001
forbids the system storing or handling passwords. That reading was revisited:
Supabase holds the credential, hashes it with bcrypt, and this application
never sees it — the backend receives a signed JWT and nothing else. The
requirement as written still covers Supabase, so **FR-ONB-001 needs amending**
before baseline. It is a specification change, not a quiet reinterpretation.

Password reset ships with it, not after it. An app that can lock someone out
over a forgotten password and offers no way back has a broken account
lifecycle, not a missing feature.

*No backend change.* Supabase issues the same JWT shape regardless of provider,
`auth_provider` is a plain `String(32)` rather than an enum, and provider
extraction is already generic. The 43 unit tests and 34 Postman assertions
passed unchanged, which is the evidence that the boundary held.

*Cost:* a fresh email address is cheaper to obtain than a fresh Google account,
so a user restricted under FR-ONB-003 can more easily create a second one. The
route already existed via a new Google account and ASM-002 already assumes
honest answers, so this weakens a soft control rather than breaking a hard one.
Also outstanding: leaked-password checking is a paid Supabase feature and is
currently off.

---

### ADR-013 — Asymmetric token verification
**IF-COMM-002, FR-ONB-001** · Accepted

Tokens are verified against Supabase's published public keys (ES256), fetched
from the project JWKS endpoint and cached per process. HS256 remains accepted
for locally minted tokens.

Supabase signs new projects asymmetrically; the shared-secret assumption the
first implementation made was simply wrong, and every real sign-in failed with
`InvalidAlgorithmError` until this was found.

Keeping HS256 is deliberate, not legacy. `scripts/make_test_token.py` and the
Postman collection both mint tokens locally from the project secret. Dropping
that path would mean the whole verification suite could only run against live
sign-ins — slow, rate limited, and dependent on an email provider that has
nothing to do with what is being tested.

The algorithm is read from the token header, which is unverified, so the
permitted set is closed. A token declaring `none` is rejected before a key is
looked up. Without that check an attacker chooses the algorithm and the
signature stops meaning anything.

*Cost:* one blocking HTTP call on first use per process. An async fetch would
need a lock to stop a cold start firing several at once — more machinery than
a once-per-process call warrants.