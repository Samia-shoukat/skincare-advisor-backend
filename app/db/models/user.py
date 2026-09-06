"""
The User entity, mapped to the SRS Section 6.2 data dictionary.

Everything onboarding writes lands in this one table. FR-ONB-002 through
FR-ONB-007 each set one or two columns here, and the scan pipeline reads almost
all of them before it will proceed.

Two design decisions are worth stating up front, because both are enforced by
what this file DOESN'T have rather than by what it does:

  * There is no image column, and there never will be one anywhere in this
    package (CON-001, DR-001).
  * There is no update path for date of birth or skin type. FR-ONB-002's
    rationale is explicit: an editable date of birth makes the FR-ONB-003 age
    gate trivially bypassable. Making the field immutable in the model, and
    exposing no PATCH endpoint, is stronger than hiding a button in the UI.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import AgeBand, SkinType, SubscriptionTier
from app.db.base import Base, new_uuid


# Postgres gets JSONB, which is binary and indexable. Anything else falls back
# to plain JSON so the same models can run against SQLite in tests -- a test
# that checks a validation rule should not need a live Supabase connection.
JSONType = JSONB().with_variant(JSON(), "sqlite")


# FR-ONB-002. The lower bound is deliberately NOT enforced here -- see the note
# at the bottom of this file.
MAX_AGE = 80


class User(Base):
    """
    SRS 6.2.

    `auth_id` is the provider-issued subject identifier and the primary key.
    That choice comes straight from FR-ONB-001's acceptance criteria: a
    returning user of the same provider account must resolve to the existing
    record, and using the provider's own identifier as the key makes a
    duplicate structurally impossible rather than merely unlikely.
    """

    __tablename__ = "users"

    # --- Identity (FR-ONB-001) --------------------------------------------
    auth_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    auth_provider: Mapped[str] = mapped_column(String(32))  # "google" | "apple"

    # --- Age (FR-ONB-002, 003, 004) ---------------------------------------
    # `date_of_birth` is Restricted/PII in 6.2 and marked "encrypted at rest".
    # Supabase encrypts the whole volume, which covers the stolen-disk case.
    # Column-level encryption is a separate decision still to be made.
    date_of_birth: Mapped[date] = mapped_column(Date)

    # Null until the user passes the confirmation screen. Once set, the date is
    # frozen: `confirm_date_of_birth` refuses a second call.
    dob_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Derived, never entered. Included in every routine request (FR-ONB-004).
    age_band: Mapped[AgeBand | None] = mapped_column(SAEnum(AgeBand, name="age_band"))

    # FR-ONB-003. Set when confirmed age is under 13. The block persists across
    # restarts because it lives here, not in client state.
    scan_access_blocked: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Safety screening (FR-ONB-005) ------------------------------------
    is_pregnant: Mapped[bool] = mapped_column(Boolean, default=False)
    on_prescription_treatment: Mapped[bool] = mapped_column(Boolean, default=False)

    # Set by a "yes" to question 3 or 4. FR-TRI-001 checks this BEFORE any image
    # is captured, so a flagged user never has a photograph taken at all.
    referral_flag: Mapped[bool] = mapped_column(Boolean, default=False)

    # All four answered. Scanning is blocked until this is true.
    safety_answers_completed: Mapped[bool] = mapped_column(Boolean, default=False)

    # --- Skin type (FR-ONB-006) -------------------------------------------
    # Null until the questionnaire is completed. "Completed once during
    # onboarding" is read strictly: no retakes.
    skin_type: Mapped[SkinType | None] = mapped_column(SAEnum(SkinType, name="skin_type"))
    skin_type_set_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Consent (FR-ONB-007) ---------------------------------------------
    # Both fields matter. The version is what makes re-acknowledgement possible
    # when the limitations statement changes.
    consent_version: Mapped[str | None] = mapped_column(String(32))
    consent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # --- Access (FR-SUB-001, 002) -----------------------------------------
    subscription_tier: Mapped[SubscriptionTier] = mapped_column(
        SAEnum(SubscriptionTier, name="subscription_tier"),
        default=SubscriptionTier.FREE,
    )

    # Server-side truth. Any count the client holds is display only (FR-SUB-002).
    scans_remaining: Mapped[int] = mapped_column(Integer, default=1)

    # --- Lifecycle ---------------------------------------------------------
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    # DR-004: 24 months without sign-in triggers notification, then deletion.
    last_sign_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    safety_answer_changes: Mapped[list["SafetyAnswerChange"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    # -----------------------------------------------------------------
    # Derived state
    # -----------------------------------------------------------------

    @property
    def onboarding_complete(self) -> bool:
        """
        SRS 4.3: all of these must be satisfied before a scan is permitted.
        The scan pipeline calls this, so adding a fifth onboarding step later
        means adding it here and nowhere else.
        """
        return (
            self.dob_confirmed_at is not None
            and self.safety_answers_completed
            and self.skin_type is not None
            and self.consent_at is not None
        )

    def consent_current(self, required_version: str) -> bool:
        """FR-ONB-007: an incremented version re-presents the screen."""
        return self.consent_at is not None and self.consent_version == required_version

    # -----------------------------------------------------------------
    # Immutability guards
    # -----------------------------------------------------------------

    def confirm_date_of_birth(self, dob: date, today: date) -> None:
        """
        FR-ONB-002 and FR-ONB-003, in one place.

        Called only after the user has seen the confirmation screen. Raises if
        called twice -- the second call is either a bug or an attempt to move
        the age gate, and both should be loud.
        """
        if self.dob_confirmed_at is not None:
            raise ValueError("date of birth is already confirmed and cannot be changed")

        if dob > today:
            raise ValueError("date of birth is in the future")

        age = compute_age(dob, today)
        if age > MAX_AGE:
            raise ValueError("date of birth is out of range")

        self.date_of_birth = dob
        self.dob_confirmed_at = datetime.now(timezone.utc)

        if age < 13:
            # FR-ONB-003. The record is kept; only scan access is withdrawn.
            self.scan_access_blocked = True
            self.age_band = None
        else:
            self.age_band = AgeBand.MINOR if age < 18 else AgeBand.ADULT

    def set_skin_type(self, skin_type: SkinType) -> None:
        """FR-ONB-006: completed once during onboarding. No retakes."""
        if self.skin_type is not None:
            raise ValueError("skin type is already set and cannot be changed")
        self.skin_type = skin_type
        self.skin_type_set_at = datetime.now(timezone.utc)


class SafetyAnswerChange(Base):
    """
    Audit trail for FR-ONB-005 answers.

    NOT in the SRS. Section 6.1 lists six entities and this is a seventh, so it
    needs adding to the data model before baseline.

    It exists because we allow safety answers to be updated -- a user whose
    wound has healed should not be locked out forever. But `referral_flag` is
    what FR-TRI-001 checks before any image is captured, so a user who can turn
    it off can walk past the only referral layer that works when clinical signal
    screening is unavailable (ASM-004). Allowing the change without recording it
    would leave no way to tell an honest correction from a bypass.

    Retention should follow DR-003 (24 months) since this is scan-adjacent
    safety data, and it hard-deletes with the account under DR-002.
    """

    __tablename__ = "safety_answer_changes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    user_auth_id: Mapped[str] = mapped_column(
        ForeignKey("users.auth_id", ondelete="CASCADE"), index=True
    )

    # {"SQ1": false, "SQ2": false, "SQ3": true, "SQ4": false}
    previous_answers: Mapped[dict | None] = mapped_column(JSONType)
    new_answers: Mapped[dict] = mapped_column(JSONType)

    # Recorded separately because this is the field with safety consequence.
    referral_flag_before: Mapped[bool | None] = mapped_column(Boolean)
    referral_flag_after: Mapped[bool] = mapped_column(Boolean)

    changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="safety_answer_changes")


def compute_age(dob: date, today: date) -> int:
    """
    Age in whole years, computed server-side.

    The client shows a computed age on the confirmation screen, but that number
    is for the user's benefit. This is the one that decides FR-ONB-003, because
    a gate the client evaluates is a gate the client can move.
    """
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))