# Copyright (c) 2026 OpenNVR
# This file is part of OpenNVR.
# 
# OpenNVR is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
# 
# OpenNVR is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU Affero General Public License
# along with OpenNVR.  If not, see <https://www.gnu.org/licenses/>.

"""
Centralized logging configuration for OpenNVR Server.
Provides comprehensive logging setup with file rotation and structured formatting.
"""

import json
import logging
import logging.handlers
from datetime import datetime
from pathlib import Path
from typing import Any

# Create logs directory if it doesn't exist
# Go up 2 levels from server/core/logging_config.py -> server/core -> server
# Then up 1 more level to project root
LOGS_DIR = Path(__file__).resolve().parents[2] / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# Define log file path
LOG_FILE = LOGS_DIR / "server.log"


class JSONFormatter(logging.Formatter):
    """Custom JSON formatter for structured logging."""

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra fields if present
        if hasattr(record, "user_id"):
            log_data["user_id"] = record.user_id
        if hasattr(record, "camera_id"):
            log_data["camera_id"] = record.camera_id
        if hasattr(record, "action"):
            log_data["action"] = record.action
        if hasattr(record, "ip_address"):
            log_data["ip_address"] = record.ip_address
        if hasattr(record, "user_agent"):
            log_data["user_agent"] = record.user_agent
        if hasattr(record, "extra_data"):
            log_data["extra_data"] = record.extra_data
        if hasattr(record, "request_id"):
            log_data["request_id"] = record.request_id

        # Add exception info if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_data, ensure_ascii=False)


def setup_logging():
    """Set up comprehensive logging configuration."""

    # Create root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Clear any existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # File handler with rotation (50MB max, keep 10 files)
    file_handler = logging.handlers.RotatingFileHandler(
        LOG_FILE,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=10,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(JSONFormatter())

    # Console handler for development
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    console_handler.setFormatter(console_formatter)

    # Add handlers to root logger
    root_logger.addHandler(file_handler)
    root_logger.addHandler(console_handler)

    # Configure specific loggers

    # OpenNVR application logger
    opennvr_logger = logging.getLogger("opennvr")
    opennvr_logger.setLevel(logging.INFO)

    # Camera operations logger
    camera_logger = logging.getLogger("opennvr.camera")
    camera_logger.setLevel(logging.INFO)

    # Recording operations logger
    recording_logger = logging.getLogger("opennvr.recording")
    recording_logger.setLevel(logging.INFO)

    # RTSP operations logger
    rtsp_logger = logging.getLogger("opennvr.rtsp")
    rtsp_logger.setLevel(logging.INFO)

    # Authentication logger
    auth_logger = logging.getLogger("opennvr.auth")
    auth_logger.setLevel(logging.INFO)

    # API request logger
    api_logger = logging.getLogger("opennvr.api")
    api_logger.setLevel(logging.INFO)

    # MediaMTX operations logger
    mediamtx_logger = logging.getLogger("opennvr.mediamtx")
    mediamtx_logger.setLevel(logging.INFO)

    # Suppress overly verbose third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)

    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a logger instance with the given name."""
    return logging.getLogger(f"opennvr.{name}")


class OpenNVRLoggerAdapter(logging.LoggerAdapter):
    """Custom logger adapter for OpenNVR application."""

    def __init__(self, logger: logging.Logger, extra: dict[str, Any] = None):
        super().__init__(logger, extra or {})

    def process(self, msg: str, kwargs: dict[str, Any]) -> tuple:
        """Process log message and add extra context."""
        extra = kwargs.get("extra", {})

        # Merge instance extra with call-time extra
        for key, value in (self.extra or {}).items():
            if key not in extra:
                extra[key] = value

        if extra:
            kwargs["extra"] = extra

        return msg, kwargs

    def log_action(
        self,
        action: str,
        level: int = logging.INFO,
        user_id: int | None = None,
        camera_id: int | None = None,
        message: str | None = None,
        extra_data: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
        request_id: str | None = None,
    ):
        """Log an action with structured data."""

        log_message = message or f"Action: {action}"

        extra = {
            "action": action,
            "user_id": user_id,
            "camera_id": camera_id,
            "ip_address": ip_address,
            "user_agent": user_agent,
            "request_id": request_id,
            "extra_data": extra_data,
        }

        # Remove None values
        extra = {k: v for k, v in extra.items() if v is not None}

        self.log(level, log_message, extra=extra)


def create_action_logger(name: str, **default_extra) -> OpenNVRLoggerAdapter:
    """Create a logger adapter with default extra context."""
    logger = get_logger(name)
    return OpenNVRLoggerAdapter(logger, default_extra)


# Module-level loggers for common use
main_logger = create_action_logger("main")
camera_logger = create_action_logger("camera")
recording_logger = create_action_logger("recording")
rtsp_logger = create_action_logger("rtsp")
auth_logger = create_action_logger("auth")
api_logger = create_action_logger("api")
mediamtx_logger = create_action_logger("mediamtx")
config_logger = create_action_logger("config")
