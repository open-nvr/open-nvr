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
    # face_identify (ai-adapter#81): one call per frame when the adapter
    # answers it; detect-then-crop otherwise. A match whose lead over the
    # runner-up is under the margin is ambiguous, not a hit.
    identify_margin: float = 0.05
    min_face_px: int = 20
    max_faces: int = 60
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
    # 0 = no limit.
    max_registered_students: int = 0
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
    # Seen first after this many minutes into the class → LATE.
    late_grace_minutes: int = 10
    # A class watched in fewer than this share of its slots is flagged and
    # its automatic marks are UNWATCHED (an outage is not an absence).
    min_coverage_ratio: float = 0.5
    # One frame per slot in which somebody was recognised goes to core's
    # evidence store, for the class review.
    keep_evidence_frames: bool = True
    planner_enforce_time_window: bool = False
    # Rooms that can hold a class at the same moment (one thread each,
    # one frame per attempt interval: cheap).
    max_workers: int = 50
    # Finalisation runs this long after the class ends, so the worker's
    # last slot is on disk first; the worker also finalises when it exits.
    finalize_grace_seconds: int = 30

    # ── Staff attendance ────────────────────────────────────────────
    staff_face_category: str = "attendly_staff"
    staff_code_prefix: str = "ST"
    staff_poll_seconds: float = 60.0
    # Default working window for a department (local time, HH:MM).
    staff_work_start: str = "09:00"
    staff_work_end: str = "18:00"

    # ── Gates (hostel blocks, the mess) ─────────────────────────────
    gate_poll_seconds: float = 1.0
    # The same person is not re-decided for this long after a decision.
    gate_cooldown_seconds: float = 8.0
    # 0 → recognition_threshold. Gates admit people, so a stricter value
    # than the classroom's is reasonable.
    gate_recognition_threshold: float = 0.0
    # "meal=HH:MM-HH:MM,..." — when each meal is served; the mess gate
    # admits only inside a window, against that meal's bookings.
    meal_windows: str = "breakfast=07:00-09:30,lunch=12:00-14:30,dinner=19:00-21:30"
    # Decisions go on the platform bus as access.decided.v1 (the
    # gate-controller app drives the barrier). Blank → not published.
    nats_url: str = Field("", validation_alias=AliasChoices("NATS_URL", "ATTENDLY_NATS_URL"))
    nats_token: str = Field("", validation_alias=AliasChoices("NATS_TOKEN", "ATTENDLY_NATS_TOKEN"))
    open_url_timeout_seconds: float = 3.0

    # ── Logging ─────────────────────────────────────────────────────
    log_level: str = "INFO"

    @property
    def effective_faces_token(self) -> str:
        return self.faces_token or self.internal_api_key

    @property
    def effective_nats_token(self) -> str:
        return self.nats_token or self.internal_api_key

    @property
    def effective_gate_threshold(self) -> float:
        return self.gate_recognition_threshold or self.recognition_threshold


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Tests change the environment between cases."""
    get_settings.cache_clear()
