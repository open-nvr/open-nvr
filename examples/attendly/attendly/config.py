"""Settings. Platform wiring comes from the same environment the OpenNVR
app overlays already set (``OPENNVR_URL``, ``KAIC_URL``,
``OPENNVR_INTERNAL_API_KEY``); everything attendly-specific is prefixed
``ATTENDLY_``."""
from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ATTENDLY_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── OpenNVR platform ────────────────────────────────────────────
    opennvr_url: str = Field(
        "http://opennvr-core:8000",
        validation_alias=AliasChoices("OPENNVR_URL", "ATTENDLY_OPENNVR_URL"),
    )
    kaic_url: str = Field(
        "http://opennvr-core:8100",
        validation_alias=AliasChoices("KAIC_URL", "OPENNVR_KAIC_URL", "ATTENDLY_KAIC_URL"),
    )
    internal_api_key: str = Field(
        "",
        validation_alias=AliasChoices(
            "OPENNVR_INTERNAL_API_KEY", "INTERNAL_API_KEY", "ATTENDLY_INTERNAL_API_KEY"
        ),
    )
    # The InsightFace adapter's own /faces/* routes (KAI-C does not proxy
    # enrolment). Token defaults to the site key, which is what the
    # adapter's OPENNVR_ADAPTER_TOKEN is set to in the apps overlay.
    faces_url: str = Field(
        "http://insightface-adapter:9005",
        validation_alias=AliasChoices("ATTENDLY_FACES_URL"),
    )
    faces_token: str = Field("", validation_alias=AliasChoices("ATTENDLY_FACES_TOKEN"))
    recognition_adapter: str = "insightface"
    face_category: str = "attendly_student"
    # Cosine similarity on InsightFace embeddings, 0..1.
    recognition_threshold: float = 0.5
    duplicate_threshold: float = 0.65
    top_k: int = 5
    platform_timeout_seconds: float = 15.0

    # ── Serving ─────────────────────────────────────────────────────
    listen_host: str = "0.0.0.0"
    port: int = 9300
    # Where the operator's browser opens the app. Blank → the catalog's
    # "{host}" placeholder on our port (how the overlay publishes it).
    public_url: str = ""
    # How core reaches our contract surface (/health, /manifest, /state).
    # Blank → http://<container hostname>:<port>.
    contract_url: str = ""
    # "opennvr": every API call needs an OpenNVR bearer (default).
    # "none": open — loopback development only.
    auth_mode: str = "opennvr"
    cors_origins: str = ""

    # ── Storage ─────────────────────────────────────────────────────
    database_url: str = "sqlite:///./data/attendly.db"
    data_dir: str = "./data"
    save_debug_frames: bool = False

    # ── Domain ──────────────────────────────────────────────────────
    timezone: str = "Asia/Kolkata"
    max_registered_students: int = 100
    student_code_prefix: str = "AT"

    # Snapshot attendance timing (unchanged from the AWS version)
    class_duration_minutes: int = 10
    class_start_delay_minutes: int = 0
    snapshot_interval_minutes: int = 2
    snapshot_start_delay_seconds: int = 0
    base_slot_minutes: int = 10
    min_snapshot_interval_seconds: int = 60
    attempt_interval_minutes: int = 2
    min_attempt_interval_seconds: int = 120
    min_schedule_duration_minutes: float = 10.0
    attendance_presence_threshold: float = 0.6
    planner_enforce_time_window: bool = False
    max_workers: int = 10

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    @property
    def effective_faces_token(self) -> str:
        return self.faces_token or self.internal_api_key


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests change the environment between cases."""
    get_settings.cache_clear()
