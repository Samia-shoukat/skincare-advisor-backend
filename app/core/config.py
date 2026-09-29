"""
Server-side configuration, read from environment variables and `.env`.

Three values here are required BY THE SRS to be configuration rather than code:

  * FR-SUB-004 -- quota enforcement is toggleable, so end-to-end tests can run
    repeatedly without recreating accounts. Production keeps it on.
  * FR-AI-004 -- which backend serves each analysis task. Moving a task between
    an internal model and the hosted provider is a config change, not a code
    change.
  * FR-REC-006 -- the rules matrix is fetched from a versioned resource at
    runtime, so a clinical correction does not wait on app store review.

Everything defaults to something safe, so the app starts on a fresh clone with
no `.env` at all. It will refuse to do anything useful, but it will start, and
`/readyz` will tell you exactly what is missing.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",  # tolerate unrelated vars in the environment
    )

    # --- Environment -------------------------------------------------------
    environment: str = "development"
    log_level: str = "INFO"

    # --- Database (IF-SW-002, Supabase managed Postgres) -------------------
    # Empty by default so the app starts without a database. /readyz reports it.
    database_url: str = ""
    supabase_url: str = ""

    # --- Auth (IF-SW-001, IF-COMM-002) -------------------------------------
    # Supabase signs access tokens with the project JWT secret. This backend
    # only verifies them; it never issues a token and never sees a password
    # (FR-ONB-001).
    jwt_secret: str = ""
    jwt_audience: str = "authenticated"

    # DR-002 and Google Play's account-deletion policy. Deleting an account
    # must remove the Supabase sign-in too, not only our rows, and that needs
    # the service-role key. SERVER ONLY: this key bypasses every Supabase
    # security rule and must never reach the mobile app or a git commit.
    supabase_service_role_key: str = ""

    # --- Support and legal -------------------------------------------------
    # FR-ONB-003 shows this to restricted users; the privacy policy and the
    # account-deletion page list it. /readyz fails in production while it is
    # still the placeholder.
    support_email: str = "support@example.com"
    # Shown in the privacy policy as the party responsible for the data.
    operator_name: str = "AI Skincare Advisor project team"

    # --- Hosted vision provider (IF-SW-003, DEP-001) -----------------------
    gemini_api_key: str = ""
    # Pinned to a specific stable model, not an alias like "gemini-flash-latest":
    # an alias can change underneath a deployment, and FR-AI-009's reasoning --
    # a measured result belongs to a specific model -- applies to hosted models
    # too. Google retires old models (gemini-2.0-flash now returns 404, which
    # failed every scan), so check the model list when a scan starts failing.
    gemini_model: str = "gemini-3.8-flash"

    # FR-CAM-004 states the Zero-Save Policy is NOT satisfied if the provider
    # retains inputs. This is asserted at startup rather than assumed, so
    # forgetting to turn retention off in the provider console fails loudly
    # instead of silently.
    provider_retention_disabled: bool = False

    # --- Analysis routing (FR-AI-004, FR-AI-010) ---------------------------
    # `analysis_plan` is the comma-separated list of tasks one scan runs.
    # COMBINED is the default and the only value that satisfies OBJ-002's
    # single analysis call per scan; splitting it into COSMETIC_CONCERNS and
    # CLINICAL_SIGNAL_SCREENING doubles the provider spend and exists for the
    # Appendix I assignment, where a trained model takes one of them.
    analysis_plan: str = "COMBINED"

    # Tasks assigned to the internal inference service. Empty in release 1.0:
    # FR-AI-010 forbids serving a task from a model with no recorded evaluation
    # meeting NFR-SAFE-008, and OI-016 records that no evaluation set
    # representative of the target population exists yet. The router falls back
    # to the hosted provider and records the reason, so naming a task here
    # before its evaluation exists changes the log line, not the behaviour.
    internal_model_tasks: str = ""

    # --- Quota (FR-SUB-001, FR-SUB-004) ------------------------------------
    quota_enforced: bool = True
    default_scan_allowance: int = 1

    # --- Clinical content --------------------------------------------------
    # Local files under app/clinical/. `rules_matrix_url` is the versioned
    # remote resource FR-REC-006 requires; empty means serve the local file,
    # which is correct in development.
    rules_matrix_path: str = "app/clinical/matrix/rules_matrix.v0.json"
    associations_path: str = "app/clinical/associations/associations.v0.json"
    questionnaire_path: str = "app/clinical/questionnaire/skin_type.v0.json"
    # FR-REC-005. Seed file for the products table; see scripts/seed_catalogue.py.
    catalogue_path: str = "app/clinical/catalogue/catalogue.v0.json"
    rules_matrix_url: str = ""
    rules_matrix_cache_ttl_seconds: int = 300

    # --- Consent (FR-ONB-007) ----------------------------------------------
    # Incrementing this re-presents the limitations screen to every user.
    consent_statement_version: str = "1.2"
    # Only a fallback label for scan logs when no matrix can be loaded at all.
    # The version in force is read from the matrix itself (FR-REC-006).
    active_matrix_version: str = "0.0.0-empty"

    @property
    def is_production(self) -> bool:
        return self.environment.lower() in {"production", "prod"}


@lru_cache
def get_settings() -> Settings:
    """
    Cached so the `.env` file is read once per process rather than on every
    request. `lru_cache` also makes this easy to override in tests.
    """
    return Settings()