# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
``opennvr-app dev`` — run an app against a simulated camera.

The gap this closes: between ``opennvr-app new`` and a working stack
there used to be Docker, a NATS broker, a KAI-C adapter and a real
camera. That is a long way to go to find out whether a rule fires. This
command runs the app in-process against synthetic inference events and
prints the alerts as they fire::

    $ opennvr-app dev
    opennvr-app dev — driveway-watch 0.1.0 (perimeter)
      camera cam-1 · person walking left → right · 1 event/s · zones: driveway
      Ctrl-C to stop.

      t=  0.0s  person  conf 0.82  at (0.05, 0.50)
      t=  1.0s  person  conf 0.79  at (0.14, 0.50)  in driveway
      …
      t= 31.0s  ALERT [HIGH] Person loitering on cam-1

Nothing here talks to a broker or to core: the app is constructed with
an in-memory dispatcher and driven through the same
``handle_event`` path a real subscription would use, so what fires here
is what fires in production for the same event.

The simulated object walks across the frame at a steady rate, which is
what makes zones, ``dwell`` and ``cooldown`` observable — a stationary
point would never enter or leave anything. ``--still`` parks it in the
middle for rules about presence rather than movement.
"""
from __future__ import annotations

import datetime as _dt
import sys
import time
from pathlib import Path
from typing import Any

from .alerts import Alert, AlertChannel, AlertDispatcher

_EPOCH = _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)


class _Printer:
    """An :class:`~.alerts.AlertChannel` that prints alerts as they fire."""

    name = "dev"

    def __init__(self) -> None:
        self.count = 0
        self.at: float = 0.0

    def send(self, alert: Alert) -> bool:
        self.count += 1
        print(f"  t={self.at:6.1f}s  ALERT [{alert.severity.upper()}] {alert.title}")
        if alert.description and alert.description != alert.title:
            print(f"            {alert.description}")
        evidence = {k: v for k, v in alert.evidence.items() if v not in (None, "")}
        if evidence:
            rendered = "  ".join(f"{k}={v}" for k, v in sorted(evidence.items()))
            print(f"            {rendered}")
        return True


def _dispatcher(channel: AlertChannel) -> AlertDispatcher:
    return AlertDispatcher(channels=[channel])


def _position(step: int, total: int, *, still: bool) -> tuple[float, float]:
    """Where the simulated object is on step ``step`` — a left-to-right
    walk across the middle of the frame, or the centre when ``still``."""
    if still:
        return 0.5, 0.5
    span = max(total - 1, 1)
    return 0.05 + 0.9 * (step / span), 0.5


def _event(step: int, x: float, y: float, *, camera: str, label: str,
           confidence: float, rate: float) -> dict[str, Any]:
    """One contract-shaped ``InferenceCompletedEvent``."""
    when = _EPOCH + _dt.timedelta(seconds=step / rate)
    return {
        "correlation_id": f"dev-{step:04d}",
        "adapter": "dev",
        "adapter_version": "0.0.0",
        "camera_id": camera,
        "model_fingerprint": "sha256:dev",
        "completed_at": when.isoformat().replace("+00:00", "Z"),
        "result": {"detections": [{
            "label": label,
            "confidence": confidence,
            "track_id": "dev-track-1",
            "bbox": {"x": max(x - 0.04, 0.0), "y": max(y - 0.08, 0.0),
                     "w": 0.08, "h": 0.16},
        }]},
    }


def run_dev(
    app_dir: Path,
    *,
    config: str | None = None,
    label: str = "person",
    camera: str = "cam-1",
    confidence: float = 0.8,
    rate: float = 1.0,
    count: int = 60,
    still: bool = False,
    fast: bool = False,
    no_zones: bool = False,
) -> int:
    """Drive the app in ``app_dir`` against a simulated camera.

    Returns a process exit code: 0 when the run completed (or was
    interrupted), 2 when the app could not be loaded."""
    from .validate import find_app_module, load_manifest

    app_dir = app_dir.expanduser().resolve()
    module_name = find_app_module(app_dir)
    if module_name is None:
        print(f"error: no app module found in {app_dir}", file=sys.stderr)
        return 2
    try:
        manifest, module = load_manifest(app_dir, module_name)
    except Exception as exc:  # noqa: BLE001 — any import error is the user's
        print(f"error: importing {module_name!r} failed: "
              f"{exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 2
    if manifest is None:
        print(f"error: {module_name!r} defines no app", file=sys.stderr)
        return 2

    printer = _Printer()
    try:
        detector = _build_detector(module, manifest, app_dir, config, printer)
    except (ValueError, OSError, TypeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    drawn = [] if no_zones else _draw_stand_in_zones(detector, manifest, camera)
    zones = _zones_on(detector, camera, manifest)
    motion = "parked in the centre" if still else "walking left → right"
    print(f"opennvr-app dev — {manifest.id} {manifest.version} ({manifest.category})")
    print(f"  camera {camera} · {label} {motion} · {rate:g} event/s"
          + (f" · zones: {', '.join(zones)}" if zones else ""))
    if drawn:
        print(f"  drew a stand-in polygon across the middle of the frame for "
              f"{', '.join(drawn)} — the operator draws the real one in the "
              f"App Catalog (--no-zones to skip).")
    print("  Ctrl-C to stop.\n")

    interval = 0.0 if fast else 1.0 / rate if rate > 0 else 0.0
    try:
        for step in range(count):
            x, y = _position(step, count, still=still)
            printer.at = step / rate if rate > 0 else float(step)
            event = _event(step, x, y, camera=camera, label=label,
                           confidence=confidence, rate=rate)
            where = _describe_zone(detector, camera, x, y)
            print(f"  t={printer.at:6.1f}s  {label}  conf {confidence:.2f}  "
                  f"at ({x:.2f}, {y:.2f}){where}")
            detector.handle_event(event)
            if interval:
                time.sleep(interval)
    except KeyboardInterrupt:
        print("\n  stopped.")
    print(f"\n  {printer.count} alert(s) fired over {count} event(s).")
    return 0


def _build_detector(module: Any, manifest: Any, app_dir: Path,
                    config: str | None, channel: AlertChannel) -> Any:
    """Construct the app's detector with an in-memory dispatcher, from
    the given config file or from sensible in-memory defaults.

    ``manifest`` comes from ``validate.load_manifest``, which finds it
    whether the app declares it at module level or only as a class
    attribute — so ``dev`` and ``validate`` never disagree about
    whether an app is runnable."""
    from .validate import app_class, facade_app

    facade = facade_app(module)
    cfg = _load_config(module, facade, app_dir, config)
    dispatcher = _dispatcher(channel)
    if facade is not None:
        return facade.build(cfg, dispatcher)
    cls = app_class(module, manifest)
    if cls is None:
        raise TypeError(
            f"found the manifest for {manifest.id!r} but no app class carrying "
            f"it — a Detector/FrameApp/AlertSubscriber subclass in this module "
            f"must set `manifest = ...`"
        )
    try:
        return cls(cfg, dispatcher)
    except TypeError as exc:
        raise TypeError(
            f"{cls.__name__} is not a Detector-shaped app, so `dev` cannot "
            f"simulate a camera for it ({exc}). `opennvr-app dev` drives "
            f"inference events; FrameApp, AlertSubscriber and "
            f"DomainEventSubscriber apps are not supported yet."
        ) from exc


def _load_config(module: Any, facade: Any, app_dir: Path, config: str | None) -> Any:
    """The parsed config: the named file, else ``config.example.yml``
    beside the app, else the generated defaults with a dummy NATS url
    (dev never connects, but the field is required)."""
    from .config import BaseAppConfig, load_app_config

    path = Path(config) if config else (app_dir / "config.example.yml")
    loader = getattr(module, "load_config", None)
    if path.exists():
        if facade is not None:
            return facade.load_config(str(path))
        if callable(loader):
            return loader(str(path))
        cfg_cls = getattr(module, "AppConfig", BaseAppConfig)
        return load_app_config(path, cfg_cls)
    if facade is not None:
        return facade.config_class()(nats_url="nats://dev:4222",
                                     subject_pattern="opennvr.inference.>")
    cfg_cls = getattr(module, "AppConfig", BaseAppConfig)
    return cfg_cls(nats_url="nats://dev:4222", subject_pattern="opennvr.inference.>")


#: The polygon `dev` draws for a zone the app declares but nobody has
#: configured — the middle third of the frame, which the simulated walk
#: crosses. Without it a `zone=` rule can never fire under `dev`, which
#: is the one example every quickstart leads with.
STAND_IN_ZONE = [[0.33, 0.0], [0.67, 0.0], [0.67, 1.0], [0.33, 1.0]]


def _declared_zones(manifest: Any) -> list[str]:
    """Zone names the app declares, from its manifest params."""
    return [p.name for p in getattr(manifest, "params", [])
            if str(getattr(p, "type", "")) == "geometry.polygon"]


def _zones_on(detector: Any, camera: str, manifest: Any) -> list[str]:
    """Zones actually resolvable for the simulated camera."""
    resolve = getattr(detector, "_zones_for", None)
    if callable(resolve):
        return sorted(resolve(camera))
    return sorted(_declared_zones(manifest))


def _draw_stand_in_zones(detector: Any, manifest: Any, camera: str) -> list[str]:
    """Give every undrawn zone a stand-in polygon on the simulated
    camera, and report which ones were filled in."""
    cfg = getattr(detector, "cfg", None)
    if cfg is None:
        return []
    filled: list[str] = []
    for name in _declared_zones(manifest):
        current = getattr(cfg, name, None)
        if isinstance(current, dict) and current.get(camera):
            continue
        if isinstance(current, (list, tuple)) and current:
            continue
        merged = dict(current) if isinstance(current, dict) else {}
        merged[camera] = [list(point) for point in STAND_IN_ZONE]
        setattr(cfg, name, merged)
        filled.append(name)
    if filled:
        cache = getattr(detector, "_facade_zones", None)
        if isinstance(cache, dict):
            cache.clear()
    return filled


def _describe_zone(detector: Any, camera: str, x: float, y: float) -> str:
    """``"  in driveway"`` when the simulated point is inside a zone."""
    resolve = getattr(detector, "_zones_for", None)
    if not callable(resolve):
        return ""
    from .geometry import Point

    point = Point(x, y)
    inside = [n for n, z in resolve(camera).items() if z.contains(point)]
    return f"  in {', '.join(inside)}" if inside else ""


__all__ = ["run_dev"]
