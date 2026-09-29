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

### ADR-014 — Capture gate runs after the shutter, not before
**FR-CAM-001, FR-CAM-002, FR-CAM-004** · Accepted

FR-CAM-001 requires the quality check to run on preview frames, before a photo
is taken. It does not, on this hardware. `frame.toArrayBuffer()` fails with
"Failed to lock HardwareBuffer for reading" on every frame — on the emulator
and on a physical TECNO device, in RGB and in YUV, with and without a resize
plugin. Some Android camera drivers do not expose a CPU-readable buffer and
vision-camera has no fallback for them.

So the gate moved: the photo is taken, shrunk natively, measured, and either
prepared for upload or discarded with a prompt. The user sees the same
instruction, one tap later.

The shrink matters as much as the move. jpeg-js is pure JavaScript, and
decoding a full camera photo blocks the JS thread for seconds — the screen
simply freezes with no error. Resizing to 32px in native code first brought the
check from ~3000ms to **~480ms**.

The live path is left wired. A phone whose driver does allow the lock gets
pre-capture feedback for free, and capture is permitted when the live gate
never ran — refusing every photo on a device that cannot deliver frames would
be worse than one unvetted shutter press.

*Cost:* one wasted tap when a photo is rejected, which is precisely what
FR-CAM-001 exists to prevent. FR-CAM-002's ≥24fps preview target could not be
measured at all, since the frame processor delivers nothing here. **Neither is
a safety regression:** no rejected image is transmitted, and the captured file
is deleted on every path including failure, so FR-CAM-004 holds.

*Also cost:* three separate failures during development presented identically —
a spinner that never stopped. Every step now runs behind a timeout, because a
hung native call, a blocking decode and a request to a missing endpoint are
indistinguishable from the screen and need different fixes.
---

### ADR-015 — Release 1.0 serves every analysis task from the hosted provider
**FR-AI-004, FR-AI-010, CON-004, CON-005** · Accepted

SRS v1.3 brought custom model training into scope. Its own open issues undercut
shipping one: OI-014 (datasets not acquired), OI-016 (no evaluation set
representative of the target population, so the NFR-SAFE-008 per-skin-tone
breakdown cannot be produced), OI-017 (eight of nine concerns have no
face-format dataset). FR-AI-010 forbids serving a task from a model without a
recorded evaluation meeting threshold. Hosted-only is therefore the *compliant*
configuration, not a shortcut around the requirement.

What ships is the thing that makes the decision reversible: one provider
interface (`app/services/analysis/base.py`), a config-driven router with the
FR-AI-010 gate and recorded fallbacks, and FR-AI-009 version recording. A
test assigns a task to the internal model in configuration and the referral
test passes unchanged — FR-AI-004's acceptance criterion, verified.

*Cost:* the training pipeline and Appendix I register are not built. When an
evaluation exists, registering an internal provider and setting
`INTERNAL_MODEL_TASKS` is the whole change.

---

### ADR-016 — Condition naming implemented, shipped disabled
**FR-AI-007, FR-AI-008, DR-009, CON-002** · Accepted

Every Appendix H row ships `enabled: false`. OI-011 (Critical) says the store
health-policy consequences of naming conditions are unassessed; OI-012 says
naming "cannot be enabled until" the table is precision-measured; DEP-003 (no
clinical reviewer) is still at risk. The app therefore behaves as SRS v1.1 did:
observation plus referral, no names. The consent statement stays accurate — it
says the app "may" list conditions.

The mechanism is complete and tested: suppression reasons, the load-time
refusal of single-name rows and of enabled rows without a measurement ≥ 0.80,
equal-prominence rendering, and the `displayed_associations` audit column.
Enabling a row is a data change plus a measurement, and one test proves a
measured, enabled row does produce names.

IRREGULAR_LESION and OPEN_WOUND have **no row at all**, permanently. Naming
anything from a phone photo of an irregular mark risks both false alarm and
false reassurance with no wording that avoids both.

---

### ADR-017 — One provider call per scan
**OBJ-002, FR-AI-004** · Accepted

The three per-task prompts already in the Gemini provider would have meant
three calls per scan against OBJ-002's "single analysis call per scan". A
COMBINED task carries both halves of Appendix G and is the default plan. The
split tasks stay, because the Appendix I assignment can only be expressed by
splitting. When a plan does split, results merge with `imageUsable` as an AND —
a clinical screen that couldn't read the photo must not let a routine through
on the concerns task alone.

---

### ADR-018 — A bad observation is replaced, never dropped
**DR-008, FR-AI-005, FR-AI-006** · Accepted

When provider text contains a condition name or a hedge ("looks like",
"consistent with"), the signal is kept and its text replaced with curated
wording from `app/clinical/copy/observations.py`. Dropping the signal would
drop the referral — the one outcome FR-AI-006 is shaped to prevent. Each
substitution is logged with the terms that caused it, so a prompt regression
shows up as a rate.

The prohibited list is split into condition names and clinical framing, because
the FR-TRI-005 summary is *required* to say "not a diagnosis" and is only
prohibited from naming conditions.

---

### ADR-019 — Cleared scans end in an honest error until the rules engine lands
**FR-SUB-003, SRS 4.2** · **Superseded by Feature 4** (routines now generated)

Stages 6–7 are Feature 4. A scan that clears triage is logged as
`ScanOutcome.ERROR` with `pendingStage: RULES_ENGINE` and returns
PROVIDER_UNAVAILABLE. ScanOutcome is closed at four values; a scan that
produced no routine and was not a referral or an unusable image is an error
from the user's side; FR-SUB-003 leaves the allowance untouched. The referral
path — the safety-bearing half — is complete and live.

---

### ADR-020 — Ingredients live in the matrix, not a database table
**SRS 6.1, CON-003, FR-REC-005** · Accepted

SRS 6.1 lists Ingredient as an entity. Appendix F already defines every
ingredient with its restriction flags, and CON-003 puts clinical logic there
only. A database copy of `pregnancyRestricted` is a second place for it to be
wrong. Products list matrix ingredient keys; flags are always read from the
matrix in force, and the catalogue is validated against it on load.
**Needs an SRS 6.1 amendment.**

---

### ADR-021 — One treatment step per concern, one active per time of day
**FR-REC-001, FR-REC-004** · Accepted, pending clinical review

Each concern gets the first permitted rule in matrix order, and the matrix
caps actives at one AM and one PM. Conservative on purpose for a first routine:
layering actives is the usual cause of irritation. The cap is a matrix value
(`limits`), so a reviewer can raise it without code.

---

### ADR-022 — The catalogue filter checks the whole bottle
**FR-REC-005** · Accepted

A product is shown only if every ingredient in it is permitted, its primary
strength is within the rule's maxPercent, it clashes with nothing else in the
routine, and it adds no second active. Otherwise a safe routine could be
undone by the product chosen to carry it. A missing tier falls back to the
generic pharmacy text and is recorded.

---

### ADR-023 — Concerns are shown by label, not identifier
**CON-002, DR-008, IF-UI-001** · Accepted

"For: ACNE" on a routine reads as a diagnosis, and "acne" is on the DR-008
list. The routine screen uses server-supplied labels ("Breakouts", "Shine").

---

### ADR-024 — Every live routine is re-checked against the invariants
**FR-REC-004, FR-REC-006** · Accepted

The test suite proves all 16,384 input combinations safe against the shipped
matrix. FR-REC-006 lets a new matrix arrive at runtime, which the tests never
saw, so each real routine is checked again before display. A failing routine
is withheld, not charged, logged at ERROR, and recorded with
`pendingStage: INVARIANT_CHECK`.

---

### ADR-025 — Frame processors are compiled out of the app
**FR-CAM-001, FR-CAM-002** · Accepted
**Extends ADR-014**

`android/gradle.properties` sets `VisionCamera_enableFrameProcessors=false`.

ADR-014 already recorded that the live pre-shutter gate delivers nothing on
this hardware, and that the gate therefore runs after the shutter. Frame
processors were still compiled in, and they drag in react-native-worklets-core,
whose `fix-prefab.gradle` forces a CMake re-run on every build. On Windows that
makes ninja fail with "manifest 'build.ninja' still dirty after 100 tries" --
it failed four consecutive release builds and no amount of cleaning helped,
because the re-run is deliberate on the library's part.

Compiling them out removes the dependency and the failure. Behaviour is
unchanged: the live path never produced a frame on any device tested.

`FRAME_PROCESSORS_ENABLED` in `lib/frameSource.ts` guards the JS side, because
passing a `frameProcessor` to `<Camera>` in such a build throws at runtime. It
reads `EXPO_PUBLIC_FRAME_PROCESSORS`, so a build that re-enables the native
side re-enables the JS side with it.

*Cost:* FR-CAM-002's ≥24fps preview target stays unverifiable, as ADR-014
already noted. Re-enabling means flipping both flags and resolving the prefab
clash, most likely by upgrading vision-camera.

---

### ADR-026 — Phone testing runs through an HTTPS tunnel, not the LAN
**FR-CAM-003, IF-COMM-001** · Accepted, temporary

A standalone release build cannot talk to `http://<laptop-ip>:8000`: Android
blocks cleartext outside debug builds, and `uploadImage.ts` refuses to send a
face photograph over anything but HTTPS once `__DEV__` is false. Both are
correct and neither should be relaxed for convenience.

So the laptop's backend is published through an SSH tunnel
(`ssh -R 80:127.0.0.1:8000 nokey@localhost.run`), which terminates TLS and
gives a public `https://` address, and that address is baked into the build via
`EXPO_PUBLIC_API_URL`.

*Cost:* the address changes whenever the tunnel restarts, so a new tunnel means
a new build. That is acceptable for a test session and not acceptable for a
demo or for users -- both need the backend deployed with a fixed hostname.

---

### ADR-027 — Normal findings are excluded from clinical signal screening
**FR-AI-005, FR-TRI-002, OI-010, ASM-001** · Accepted, **needs clinical review**

Live testing on a real face referred three scans out of three. Every one was
the same finding: darker skin under and around the eyes. Gemini was right --
that is an area darker than the skin around it, which is what the Appendix G
prompt asked for -- and the pipeline did exactly what FR-TRI-002 says: a signal
is present, so no routine.

The result is an app that refers nearly every user and shows almost nobody a
routine. Those scans also carried real cosmetic findings (excess oil, uneven
tone, post-acne marks) that were discarded with the referral.

A screening layer that fires on almost everyone is not a safety feature. It
buries the referral that matters and teaches users to ignore the screen.

So the prompt now excludes findings that are ordinary or cosmetic: dark
circles, post-blemish marks, freckles and ordinary moles, generally uneven
tone, discolouration confined to a blemish, and photographic shadow. It also
asks for a *discrete patch with a defined edge, not mirrored on both sides*,
rather than any darker area -- symmetry is what separates normal periorbital
shading from a patch worth looking at.

Nothing else changed: presence still triggers referral, referral still suppresses
the routine, and quota is still untouched (FR-TRI-004).

*Cost, and it is real:* this narrows what the screen can catch. Periorbital
darkening can occasionally accompany conditions this app does not attempt to
detect, and excluding it by name means such a case reaches a routine instead of
a referral. FR-ONB-005's declared answers remain the layer that catches what
the image misses. **A practitioner must confirm this exclusion list before
release** (OI-010 covers exactly this: screening reliability is unmeasured).
