# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
Generic YAML config helpers.

Deliberately thin: the SDK loads and shape-checks the YAML document;
each app keeps its own typed parse (``load_config(path) -> AppConfig``)
because config semantics — which keys exist, their defaults, their
validation messages — are app business logic that the app's own tests
pin down. Once manifests drive config (spec §05), ``manifest.params``
becomes the validator and these helpers stay as the file-loading edge.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_yaml(path: str | Path) -> dict[str, Any]:
    """Read + parse a YAML config file, requiring a mapping at the root.

    Raises ``ValueError`` on a non-mapping root and lets ``OSError``
    from the read propagate — callers surface both as operator-facing
    config errors and exit non-zero."""
    raw = yaml.safe_load(Path(path).read_text())
    if not isinstance(raw, dict):
        raise ValueError(f"config {str(path)!r}: root must be a mapping")
    return raw


def require(cfg: dict[str, Any], key: str, *, path: str = "config") -> Any:
    """Fetch a required config value, rejecting missing / empty values.

    ``path`` names the config source in the error message (file path or
    a nested-section breadcrumb like ``"config: cameras[0]"``)."""
    value = cfg.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ValueError(f"{path}: {key!r} is required")
    return value


# ── The config every app shares ───────────────────────────────────────
#
# Every app used to re-declare the same dozen keys — the NATS endpoint,
# the alert fan-out, the contract server, the registry — and re-parse
# them from YAML. ``BaseAppConfig`` is that block once; an app extends
# it with its own fields and ``load_app_config`` fills both from one
# file, with the same errors the runners already print.

from dataclasses import MISSING, dataclass, fields  # noqa: E402

DEFAULT_ALERT_SUBJECT_PREFIX = "opennvr.alerts"


@dataclass
class BaseAppConfig:
    """What the SDK reads from ``cfg`` — the runners, the NATS loop, the
    alert dispatcher, the contract server and the registry client all
    look these up by name. Subclass and add your own fields::

        @dataclass
        class AppConfig(BaseAppConfig):
            watch_labels: list[str] = field(default_factory=lambda: ["person"])
            dwell_s: float = 30.0

        cfg = load_app_config("config.yml", AppConfig)
    """

    #: NATS endpoint of the stack's event bus (required).
    nats_url: str
    #: The stack's INTERNAL_API_KEY when NATS is token-auth'd.
    nats_token: str | None = None
    #: Subject(s) to subscribe to. Detectors default to the inference
    #: broadcast; event subscribers derive it from ``subscriptions``.
    subject_pattern: str | None = None
    # Alert fan-out — stdout is always on; these are opt-in channels.
    webhook_url: str | None = None
    nats_alerts_url: str | None = None
    nats_alerts_token: str | None = None
    nats_alerts_subject_prefix: str = DEFAULT_ALERT_SUBJECT_PREFIX
    # App contract (/health /manifest /state /actions) + registry.
    contract_port: int | None = None
    contract_bind_host: str | None = None
    contract_host: str | None = None
    opennvr_url: str | None = None
    opennvr_token: str | None = None
    config_poll_seconds: float | None = None


_BASE_FIELDS = {f.name for f in fields(BaseAppConfig)}


def parse_base_config(raw: dict[str, Any], *, path: str = "config") -> dict[str, Any]:
    """The validated kwargs for :class:`BaseAppConfig` out of a raw
    mapping — the checks every app's loader used to repeat."""
    out: dict[str, Any] = {}
    out["nats_url"] = str(require(raw, "nats_url", path=path)).strip()
    if "subject_pattern" in raw and raw["subject_pattern"] is not None:
        subject = str(raw["subject_pattern"]).strip()
        if not subject:
            raise ValueError(f"{path}: 'subject_pattern' must not be empty")
        out["subject_pattern"] = subject
    for key in ("nats_token", "webhook_url", "nats_alerts_url", "nats_alerts_token",
                "contract_bind_host", "contract_host", "opennvr_url", "opennvr_token"):
        if raw.get(key):
            out[key] = str(raw[key])
    if raw.get("nats_alerts_subject_prefix"):
        out["nats_alerts_subject_prefix"] = str(raw["nats_alerts_subject_prefix"])
    if raw.get("contract_port") is not None:
        try:
            out["contract_port"] = int(raw["contract_port"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{path}: 'contract_port' must be an integer") from exc
    if raw.get("config_poll_seconds") is not None:
        out["config_poll_seconds"] = float(raw["config_poll_seconds"])
    return out


def load_app_config(path: str | Path, cls: type = BaseAppConfig):
    """Load ``path`` into ``cls`` — :class:`BaseAppConfig` or a dataclass
    subclass of it. Base keys get the standard validation; every extra
    field of ``cls`` is taken from the file by name when present, from
    its default otherwise, and a field with no default is required.
    Put app-specific checks in ``__post_init__``; raise ``ValueError``
    with a message an operator can act on."""
    raw = load_yaml(path)
    label = f"config {str(path)!r}"
    kwargs = parse_base_config(raw, path=label)
    for f in fields(cls):
        if f.name in _BASE_FIELDS:
            continue
        if f.name in raw and raw[f.name] is not None:
            kwargs[f.name] = raw[f.name]
        elif f.default is MISSING and f.default_factory is MISSING:  # type: ignore[attr-defined]
            raise ValueError(f"{label}: {f.name!r} is required")
    try:
        return cls(**kwargs)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label}: {exc}") from exc
