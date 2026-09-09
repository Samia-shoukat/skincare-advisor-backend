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

    # --- Hosted vision provider (IF-SW-003, DEP-001) -----------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # FR-CAM-004 states the Zero-Save Policy is NOT satisfied if the provider
    # retains inputs. This is asserted at startup rather than assumed, so
    # forgetting to turn retention off in the provider console fails loudly
    # instead of silently.
    provider_retention_disabled: bool = False

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
    rules_matrix_url: str = ""
    rules_matrix_cache_ttl_seconds: int = 300

    # --- Consent (FR-ONB-007) ----------------------------------------------
    # Incrementing this re-presents the limitations screen to every user.
    consent_statement_version: str = "1.2"
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