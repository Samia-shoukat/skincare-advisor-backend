"""
The consultation summary. FR-TRI-005.

"The purpose of a referral is a better-informed consultation. A record of what
was seen and when serves that purpose; a condition label does not, and risks
misdirecting it."

So the summary carries three things -- the observation text, the scan date, the
declared safety answers -- plus the fixed statement that it is not a diagnosis.
It carries no condition name, no ingredient, and no product.

Built server-side and returned as finished text, rather than assembled by the
client from parts. The copied text is what leaves the app and lands in front of
a clinician, and FR-TRI-005's acceptance criteria are about that text. Building
it in one place means the test that checks it is checking what the user copies.

## Association lists are not in the summary

Even when FR-AI-007 permits one on screen. The screen frames a differential with
fixed text ("commonly associated with... only a dermatologist can determine");
a copied summary loses its framing the moment it is pasted somewhere else, and
a bare list of names in a message to a GP reads as a self-diagnosis. FR-TRI-005
says "contains no condition name", without an exception for the differential,
and this is why.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clinical.copy import strings
from app.clinical.copy.prohibited_terms import find_condition_names
from app.db.models.user import SafetyAnswerChange, User


async def latest_safety_answers(session: AsyncSession, user: User) -> dict[str, bool]:
    """
    The four answers as last declared.

    Read from the audit trail rather than the user row, because the row keeps
    only the derived flags -- `referral_flag` is set by question 3 *or* 4, and
    a summary saying "yes to one of them" is less useful to a clinician than
    saying which.
    """
    result = await session.execute(
        select(SafetyAnswerChange)
        .where(SafetyAnswerChange.user_auth_id == user.auth_id)
        .order_by(SafetyAnswerChange.changed_at.desc(), SafetyAnswerChange.id.desc())
        .limit(1)
    )
    change = result.scalar_one_or_none()
    answers = dict(change.new_answers) if change else {}
    return {qid: bool(answers.get(qid, False)) for qid in strings.SUMMARY_ANSWER_LABELS}


def build_summary(
    *,
    observations: list[str],
    scanned_at: datetime,
    answers: dict[str, bool],
) -> str:
    """
    The copyable text.

    `observations` is empty for a referral raised by the safety answers alone
    (FR-TRI-001), in which case no photo was taken and the summary says so
    rather than leaving a blank section a reader might take for "nothing seen".
    """
    lines: list[str] = [
        strings.SUMMARY_TITLE,
        f"{strings.SUMMARY_DATE_LABEL}: {scanned_at.date().isoformat()}",
        "",
        f"{strings.SUMMARY_OBSERVED_LABEL}:",
    ]

    if observations:
        lines.extend(f"- {text}" for text in observations)
    else:
        lines.append(strings.SUMMARY_NO_PHOTO)

    lines.append("")
    lines.append(f"{strings.SUMMARY_ANSWERS_LABEL}:")
    for qid, label in strings.SUMMARY_ANSWER_LABELS.items():
        value = strings.SUMMARY_YES if answers.get(qid) else strings.SUMMARY_NO
        lines.append(f"- {label}: {value}")

    lines.append("")
    lines.append(strings.SUMMARY_NOT_A_DIAGNOSIS)

    text = "\n".join(lines)

    # Belt and braces. Observations have already passed DR-008 upstream and
    # the labels are fixed, so this should never fire -- but the summary is
    # the one output that leaves the app, and a condition name in it is a
    # requirement violation that a user can forward to anyone.
    names = find_condition_names(text)
    if names:
        raise ValueError(f"referral summary contains condition names: {names}")

    return text
