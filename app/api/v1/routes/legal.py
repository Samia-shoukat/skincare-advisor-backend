"""
Public legal pages: privacy policy, terms of use, account deletion.

Served as plain HTML with no sign-in, because Google Play needs public URLs for
the privacy policy and for account deletion, and the app opens the same pages in
an in-app browser. Mounted at the root (/legal/...), not under /v1: these are
documents, not API responses, and their URLs must not change with an API version.

## Written from what the system actually does

Every statement here describes behaviour implemented in this codebase: the
Zero-Save Policy (CON-001), the retention periods (DR-002, DR-003, DR-004), the
processors actually called (Supabase, Google Gemini), the under-13 block
(FR-ONB-003). If the code changes, these pages must change with it -- a privacy
policy that describes a different system is worse than none.

## The draft banner

The policy says Google does not keep or train on the photo. That is only true on
a paid Gemini tier with retention off, which is what
`PROVIDER_RETENTION_DISABLED` asserts. Until it is set, every page carries a
visible "draft, do not publish" banner, so an unfulfilled promise cannot be
published by accident.

THESE ARE DRAFTS, NOT LEGAL ADVICE. They need review before publication.
"""

from __future__ import annotations

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.api.deps import SettingsDep
from app.core.config import Settings

router = APIRouter(prefix="/legal", tags=["legal"])

LAST_UPDATED = "22 September 2026"


def _page(title: str, body: str, settings: Settings) -> HTMLResponse:
    banner = ""
    if not settings.provider_retention_disabled:
        banner = (
            '<div class="banner">DRAFT &mdash; do not publish. This policy promises the '
            "analysis provider does not keep photos; that is not yet confirmed "
            "(PROVIDER_RETENTION_DISABLED is false).</div>"
        )
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{escape(title)}</title>
<style>
  body {{ font: 16px/1.6 system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         color: #3E3226; background: #F5F4EA; margin: 0; padding: 0 16px 48px; }}
  main {{ max-width: 720px; margin: 0 auto; }}
  h1 {{ font-size: 28px; margin: 32px 0 4px; }}
  h2 {{ font-size: 19px; margin: 28px 0 8px; }}
  .updated {{ color: #7A6E5F; font-size: 14px; }}
  .banner {{ background: #AF5340; color: #fff; padding: 12px 16px; margin: 0 -16px;
             font-weight: 600; }}
  .note {{ background: #F8EFDB; border-left: 3px solid #C4882F; padding: 12px 16px;
           border-radius: 8px; }}
  a {{ color: #4E6B4A; }}
  li {{ margin: 4px 0; }}
</style></head>
<body>{banner}<main>
<h1>{escape(title)}</h1>
<p class="updated">Last updated {LAST_UPDATED}</p>
{body}
</main></body></html>"""
    return HTMLResponse(html)


def _contact(settings: Settings) -> str:
    email = escape(settings.support_email)
    return f'<a href="mailto:{email}">{email}</a>'


@router.get("/privacy", response_class=HTMLResponse)
async def privacy_policy(settings: SettingsDep) -> HTMLResponse:
    operator = escape(settings.operator_name)
    contact = _contact(settings)
    body = f"""
<p class="note">This app is not a medical device and does not diagnose. It suggests
over-the-counter cosmetic products.</p>

<h2>Who we are</h2>
<p>This app is operated by {operator}. Contact us at {contact}.</p>

<h2>Your photo</h2>
<ul>
  <li>Your face photo is used for one thing: to look at your skin and suggest a routine.</li>
  <li><strong>We never store it.</strong> It is held in memory while it is analysed and then
      discarded. It is not saved on your phone, on our servers, in logs, or in backups.</li>
  <li>To analyse it, we send it securely to Google's Gemini service. We use Gemini under
      terms that do not allow Google to keep your photo or use it to train its models.</li>
  <li>Photos are never used to train our own models.</li>
</ul>

<h2>What we keep</h2>
<ul>
  <li><strong>Account:</strong> your sign-in (email, or your Google account identifier),
      handled by our provider Supabase. We never see your password.</li>
  <li><strong>Date of birth:</strong> to apply age-appropriate ingredient rules and to
      restrict under-13s.</li>
  <li><strong>Your safety answers:</strong> whether you are pregnant or breastfeeding, use a
      prescription acne treatment, have wounds or a changing mole, or have certain skin
      diagnoses. This is health information. We use it only to keep suggestions safe and to
      tell you when to see a professional instead.</li>
  <li><strong>Skin type questionnaire result</strong> and a record of when you accepted the
      app's limitations.</li>
  <li><strong>Scan results:</strong> the skin characteristics found (for example "dryness"), any
      description of something a doctor should look at, and your routine. Not the photo.</li>
  <li><strong>Technical logs</strong> needed to run and secure the service. These contain no
      photos.</li>
</ul>

<h2>Who we share it with</h2>
<ul>
  <li><strong>Supabase</strong> hosts our database and sign-in.</li>
  <li><strong>Google</strong> analyses your photo (Gemini), and signs you in if you choose
      Google sign-in.</li>
</ul>
<p>We do not sell your data, show ads, or share it for marketing.</p>

<h2>How long we keep it</h2>
<ul>
  <li>Your account data, until you delete your account.</li>
  <li>Scan records: 24 months, then deleted automatically.</li>
  <li>If you don't sign in for 24 months, your account is deleted.</li>
  <li>When you delete your account, everything is deleted immediately and permanently.</li>
</ul>

<h2>Your choices</h2>
<ul>
  <li><strong>Delete your account</strong> any time in the app: Account &amp; privacy &rarr;
      Delete your account. Or see <a href="/legal/account-deletion">how to request deletion</a>.</li>
  <li>To get a copy of your data or correct it, email {contact}. For safety reasons your date
      of birth can't be changed in the app; contact us if it was entered wrongly.</li>
  <li>You can change your safety answers in the app.</li>
</ul>

<h2>Children</h2>
<p>People under 13 cannot use the scan feature. If you are 13 to 17, the app applies extra
ingredient restrictions for your age.</p>

<h2>Security</h2>
<p>All traffic is encrypted (HTTPS). Data is stored encrypted by our hosting provider.</p>

<h2>Changes</h2>
<p>If this policy changes, we will update the date above and, for important changes, tell you
in the app.</p>
"""
    return _page("Privacy policy", body, settings)


@router.get("/terms", response_class=HTMLResponse)
async def terms_of_use(settings: SettingsDep) -> HTMLResponse:
    contact = _contact(settings)
    body = f"""
<h2>Not medical advice</h2>
<p>This app is not a medical device. It cannot diagnose you. It suggests over-the-counter
cosmetic products based on your answers and what is visible in your photo, using rules based on
published clinical guidelines. If it sees something cosmetics cannot help with, it describes what
it saw and asks you to see a healthcare professional; that description is not a diagnosis and may
be wrong. If your skin is painful, bleeding, spreading, or changing quickly, see a healthcare
professional.</p>

<h2>Using products</h2>
<p>Read each product's label and follow its instructions. Introduce one new product at a time and
stop using anything that stings, burns, or irritates. Product availability and formulations can
change; check the label before you buy.</p>

<h2>Your account</h2>
<p>Each account includes one free scan with a full routine. Give accurate answers to the safety
questions; the app relies on them to keep suggestions safe. One account per person.</p>

<h2>Acceptable use</h2>
<p>Only scan your own face, or someone else's with their permission. Don't try to get around the
app's limits or safety checks, or use it to harm others.</p>

<h2>Availability and liability</h2>
<p>The app is provided as is. We do our best to keep it accurate and available, but we can't
guarantee it will always be either. To the extent the law allows, we are not liable for reactions
to products you choose to use.</p>

<h2>Contact</h2>
<p>{contact}</p>
"""
    return _page("Terms of use", body, settings)


@router.get("/account-deletion", response_class=HTMLResponse)
async def account_deletion(settings: SettingsDep) -> HTMLResponse:
    contact = _contact(settings)
    body = f"""
<h2>In the app (immediate)</h2>
<ol>
  <li>Open the app and sign in.</li>
  <li>Tap <strong>Account &amp; privacy</strong>.</li>
  <li>Tap <strong>Delete your account</strong> and confirm.</li>
</ol>

<h2>By email</h2>
<p>If you can't use the app, email {contact} from the address you signed up with and ask us to
delete your account. We will delete it within 30 days and confirm by email.</p>

<h2>What is deleted</h2>
<p>Everything: your sign-in, date of birth, safety answers, skin type, consent record, scan
history, and routine. Nothing is kept. Photos are never stored, so there are none to delete.</p>
"""
    return _page("Delete your account", body, settings)
