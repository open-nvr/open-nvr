# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
The app contract surface — spec §03, mirroring the adapter contract.

Adapters self-register with KAI-C and expose ``/health`` +
``/capabilities``; SDK apps do the identical thing against the OpenNVR
app registry (``server/routers/apps.py``). Two pieces live here:

* :class:`ContractServer` — a stdlib-only (``http.server``) HTTP
  server on a daemon thread serving the three contract endpoints:

  - ``GET /health``   → ``{"ready", "uptime_s", "events_seen",
    "alerts_fired", "last_event_age_s"}`` — powers the catalog status
    dot and stall detection;
  - ``GET /manifest`` → the static ``AppManifest.to_dict()`` — powers
    the catalog card + auto-generated config form;
  - ``GET /state``    → the app-provided live snapshot
    (:meth:`ContractMixin.state_snapshot`, default ``{}``) — powers
    the per-app dashboard.

* :class:`ContractMixin` — the counters + lifecycle glue the
  ``Detector`` / ``FrameApp`` bases inherit. Everything is **off by
  default**: the server only starts when the app config carries a
  ``contract_port`` (int), and self-registration only fires when it
  carries an ``opennvr_url``.

Config keys read (all via ``getattr``, all optional):

``contract_port``
    Port for the contract server. ``0`` binds an ephemeral port (the
    actual port is advertised at registration time). Absent ⇒ no
    server, no registration.
``contract_bind_host``
    Interface to bind (default ``0.0.0.0``).
``contract_host``
    Hostname advertised in the registration URL (default: this
    machine's ``socket.gethostname()`` — in Docker that is the
    container/service name the registry can reach).
``opennvr_url``
    Base URL of the OpenNVR backend. When set, ``run()`` POSTs
    ``{opennvr_url}/api/v1/apps/register`` on boot — best-effort: a
    down/refusing registry logs a warning and the app keeps running.
``opennvr_token``
    Optional token for the registration call (the registry routes are
    auth-gated). Sent BOTH as a bearer ``Authorization`` header (for
    user-JWT tokens) and as ``X-Internal-Api-Key`` (the deployment's
    ``INTERNAL_API_KEY``, which the register route accepts as a
    service credential). Falls back to the
    ``OPENNVR_INTERNAL_API_KEY`` environment variable when the config
    key is absent — the natural fit for compose deployments.
``config_poll_seconds``
    Interval for LIVE CONFIG DELIVERY (default ``10``; ``0`` or
    negative disables). When ``opennvr_url`` is set, the app polls
    ``GET {opennvr_url}/api/v1/apps/{manifest.id}/config`` — the
    registry is the single source of truth (spec §05) — and calls
    :meth:`ContractMixin.on_config_update` with the config dict on the
    FIRST successful fetch and on every change after. Override the
    hook to apply params live (it must be idempotent — the first call
    usually re-delivers what the boot config already set); the default
    logs that a restart is needed. Poll chosen over push: it works
    with no new inbound surface on the app and no broker dependency,
    and the app already holds the registry URL + key for registration.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

import httpx

from ._version import __version__ as _sdk_version
from .credentials import AppCredentials
from .usercontext import (
    USER_CONTEXT_HEADER, bind_user, signing_secret, unbind_user,
    verify_user_context,
)
from .usercontext import current_user as _current_user

logger = logging.getLogger(__name__)

# Budget for the one-shot registration POST at boot. Tight on purpose:
# a slow registry must not stall app startup for long.
REGISTER_TIMEOUT_SECONDS = 5.0

# Live config delivery defaults: how often the app polls its own config
# from the registry, and the per-request budget. The poll thread is a
# daemon and every failure path is swallowed-and-logged — config
# delivery must never take the app down.
CONFIG_POLL_DEFAULT_SECONDS = 10.0
CONFIG_POLL_TIMEOUT_SECONDS = 5.0

# Ceiling on an action POST body. Most action params are small operator
# form fields (a search query, a plate number), but some carry an
# uploaded image (smart-doorbell's face enrollment) — a base64 photo is
# a few MB. 8 MB accommodates that while still bounding a forged
# Content-Length from forcing an arbitrary-size read into memory on this
# (internal-network) surface.
ACTION_BODY_MAX_BYTES = 8 * 1024 * 1024

# Captured at import so the contract counters keep reading the REAL
# clock even when an app's tests monkeypatch ``time.monotonic`` to
# drive their domain state machines (package-delivery's linger test
# feeds a finite fake-clock iterator — operational bookkeeping must
# never consume ticks from the app's timeline).
_monotonic = time.monotonic


# ── The HTTP server ────────────────────────────────────────────────


class _ContractRequestHandler(BaseHTTPRequestHandler):
    """Routes GETs to the callables installed on the server instance.

    Anything that isn't one of the three contract paths is a JSON 404;
    a raising snapshot callable is a JSON 500 — the server never takes
    the app down."""

    server: "_ContractHTTPServer"  # type: ignore[assignment]

    def do_GET(self) -> None:  # noqa: N802 — http.server API
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        # RFC-0002 Phase 4 app-surface convention: GET /ui is the one
        # HTML route — core proxies /api/v1/apps/{id}/ui here. Optional
        # (apps opt in via a ui callable); everything else stays JSON.
        if path == "/ui" and getattr(self.server, "ui", None) is not None:
            bound = bind_user(self._user_context())
            try:
                html = self.server.ui()
            except Exception:
                logger.exception("contract /ui failed")
                self._send_json(500, {"error": "internal error"})
                return
            finally:
                unbind_user(bound)
            payload = str(html).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        route = self.server.routes.get(path)
        if route is None:
            self._send_json(404, {"error": f"unknown path {path!r}"})
            return
        try:
            body = route()
        except Exception:
            logger.exception("contract endpoint %s failed", path)
            self._send_json(500, {"error": "internal error"})
            return
        self._send_json(200, body)

    def do_POST(self) -> None:  # noqa: N802 — http.server API
        """``POST /actions/{name}`` — the only POST surface, and the
        only WRITE this server exposes (health/manifest/state are
        reads). When the app knows the deployment token it REQUIRES it
        (``X-Internal-Api-Key``, constant-time compared): the server's
        JWT-gated proxy forwards it, and an arbitrary process on the
        internal network without the key gets a 401 instead of a free
        verb. See the module docstring for the full (layered) boundary.

        Routed to the action dispatcher the mixin installs;
        declared-but-unknown names are the dispatcher's KeyError → 404,
        bad params its ValueError → 400, anything else a 500. The
        server itself never interprets action semantics."""
        path = self.path.split("?", 1)[0].rstrip("/")
        action = getattr(self.server, "action", None)
        if action is None or not path.startswith("/actions/"):
            self._send_json(404, {"error": f"unknown path {path!r}"})
            return
        name = path[len("/actions/"):]
        if not name or "/" in name:
            self._send_json(404, {"error": f"unknown action path {path!r}"})
            return
        expected_token = getattr(self.server, "action_token", None)
        if expected_token:
            import hmac

            presented = self.headers.get("X-Internal-Api-Key") or ""
            if not hmac.compare_digest(str(presented), str(expected_token)):
                self._send_json(
                    401, {"error": "action requires X-Internal-Api-Key"}
                )
                return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length < 0 or length > ACTION_BODY_MAX_BYTES:
                self._send_json(
                    413,
                    {"error": f"action body over {ACTION_BODY_MAX_BYTES}B cap"},
                )
                return
            raw = self.rfile.read(length) if length else b"{}"
            params = json.loads(raw.decode("utf-8") or "{}")
            if not isinstance(params, dict):
                raise ValueError("action body must be a JSON object")
        except (ValueError, RecursionError) as exc:
            # RecursionError: json.loads on absurdly nested input — the
            # same "bad body" class, and it must not kill the handler.
            self._send_json(400, {"error": f"bad action body: {exc}"})
            return
        bound = bind_user(self._user_context())
        try:
            result = action(name, params)
        except KeyError:
            self._send_json(404, {"error": f"unknown action {name!r}"})
            return
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return
        except Exception:
            logger.exception("action %s failed", name)
            self._send_json(500, {"error": "internal error"})
            return
        finally:
            unbind_user(bound)
        self._send_json(200, result if result is not None else {})

    def _user_context(self):
        """The operator core says is behind this request (verified
        against this app's key), or None — see usercontext.py."""
        secret_for = getattr(self.server, "user_secret", None)
        secret = secret_for() if callable(secret_for) else None
        return verify_user_context(
            self.headers.get(USER_CONTEXT_HEADER), secret,
            audience=getattr(self.server, "app_id", None))

    def _send_json(self, status: int, body: Any) -> None:
        payload = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Health polls arrive every few seconds; route them to DEBUG
        # instead of BaseHTTPRequestHandler's stderr spam.
        logger.debug("contract server: " + format, *args)


class _ContractHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    routes: dict[str, Callable[[], Any]]
    # Optional POST /actions/{name} dispatcher: (name, params) -> result.
    action: "Callable[[str, dict[str, Any]], Any] | None"
    # When set, POST /actions requires this X-Internal-Api-Key value.
    action_token: "str | None"
    # Optional GET /ui HTML renderer (RFC-0002 Phase 4). None = no UI.
    ui: "Callable[[], str] | None"
    # Verifies X-OpenNVR-User: () -> the signing secret (sha256 of the
    # app key) or None, plus the manifest id the token must be for.
    user_secret: "Callable[[], str | None] | None"
    app_id: "str | None"


class ContractServer:
    """Serve the §03 contract endpoints on a background daemon thread.

    stdlib-only by design (``http.server``) — the contract surface must
    not drag a web framework into every 60-line detector. Three GETs a
    few times a second is comfortably inside ``ThreadingHTTPServer``
    territory.
    """

    def __init__(
        self,
        *,
        health: Callable[[], dict[str, Any]],
        manifest: Callable[[], dict[str, Any]],
        state: Callable[[], dict[str, Any]],
        action: "Callable[[str, dict[str, Any]], Any] | None" = None,
        action_token: "str | None" = None,
        ui: "Callable[[], str] | None" = None,
        user_secret: "Callable[[], str | None] | None" = None,
        app_id: "str | None" = None,
        host: str = "0.0.0.0",
        port: int = 0,
    ) -> None:
        self._host = host
        self._requested_port = int(port)
        self._routes = {"/health": health, "/manifest": manifest, "/state": state}
        self._action = action
        self._action_token = action_token
        self._ui = ui
        self._user_secret = user_secret
        self._app_id = app_id
        self._server: _ContractHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        """The actually-bound port (resolves ``port=0`` ephemerals)."""
        if self._server is None:
            return self._requested_port
        return self._server.server_address[1]

    def start(self) -> "ContractServer":
        if self._server is not None:
            return self
        server = _ContractHTTPServer(
            (self._host, self._requested_port), _ContractRequestHandler
        )
        server.routes = self._routes
        server.action = self._action
        server.action_token = self._action_token
        server.ui = self._ui
        server.user_secret = self._user_secret
        server.app_id = self._app_id
        self._server = server
        self._thread = threading.Thread(
            target=server.serve_forever,
            name="opennvr-app-contract",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=3.0)


# ── The base-class glue ────────────────────────────────────────────


class ContractMixin:
    """Counters + contract lifecycle shared by ``Detector`` and
    ``FrameApp``. The bases call :meth:`_contract_init` at
    construction, :meth:`_contract_note_event` /
    :meth:`_contract_note_alerts` from their loops, and bracket their
    ``run()`` with :meth:`start_contract_server` /
    :meth:`register_with_opennvr` / :meth:`stop_contract_server`.
    """

    # Set by the concrete bases.
    cfg: Any
    manifest: Any

    def _contract_init(self) -> None:
        self._started_monotonic = _monotonic()
        self._events_seen = 0
        self._alerts_fired = 0
        self._last_event_monotonic: float | None = None
        self._contract_server: ContractServer | None = None
        # Live config delivery (registry poll) state.
        self._config_poll_thread: threading.Thread | None = None
        self._config_poll_stop = threading.Event()
        self._applied_config: dict[str, Any] | None = None
        self._config_update_warned = False

    def _contract_note_event(self) -> None:
        self._events_seen += 1
        self._last_event_monotonic = _monotonic()

    def _contract_note_alerts(self, count: int) -> None:
        if count > 0:
            self._alerts_fired += count

    # ── App surface ────────────────────────────────────────────────

    def state_snapshot(self) -> dict[str, Any]:
        """Override to expose live standing state (active tracks, zone
        levels, running counters) via ``GET /state``. Must return a
        JSON-serializable dict; called from the contract server's
        thread, so keep it a cheap read of existing state."""
        return {}

    def health_snapshot(self) -> dict[str, Any]:
        """The ``GET /health`` payload (spec §03)."""
        now = _monotonic()
        last_age = (
            None
            if self._last_event_monotonic is None
            else round(now - self._last_event_monotonic, 3)
        )
        return {
            "ready": True,
            "uptime_s": round(now - self._started_monotonic, 3),
            "events_seen": self._events_seen,
            "alerts_fired": self._alerts_fired,
            "last_event_age_s": last_age,
        }

    def manifest_snapshot(self) -> dict[str, Any]:
        """The ``GET /manifest`` payload — ``{}`` for manifest-less
        apps (mid-migration) rather than a 500."""
        return self.manifest.to_dict() if self.manifest is not None else {}

    def on_action(self, name: str, params: dict[str, Any]) -> Any:
        """Override to implement the verbs the manifest ``actions``
        declare (search footage, enroll a face, …). Called from the
        contract server's thread with the operator's params — the
        server-side proxy has already checked the caller is a user
        (JWT, never the service key) and validated params against the
        declared ``Action.params``.

        Raise ``KeyError(name)`` for names you don't handle (→ 404) and
        ``ValueError`` for bad params (→ 400). Return a JSON-serializable
        result; a list-of-dicts under ``"results"`` renders as a table
        in the catalog."""
        raise KeyError(name)

    @property
    def current_user(self):
        """The operator behind the ``/ui`` view or action being served
        (:class:`opennvr_app_sdk.UserContext`), or ``None`` when core
        forwarded no identity. Valid only inside ``ui_html()`` /
        ``on_action()``; ``None`` elsewhere."""
        return _current_user()

    def _dispatch_action(self, name: str, params: dict[str, Any]) -> Any:
        """Gate + dispatch: only manifest-DECLARED actions reach
        on_action — an undeclared name 404s even if a handler would
        have matched, so the manifest stays the single source of truth
        for what operators can invoke."""
        declared = {
            a.name for a in getattr(self.manifest, "actions", None) or ()
        }
        if name not in declared:
            raise KeyError(name)
        return self.on_action(name, dict(params))

    # ── Lifecycle ──────────────────────────────────────────────────

    def start_contract_server(self) -> ContractServer | None:
        """Start the contract server iff ``cfg.contract_port`` is set.
        Idempotent; returns the running server (or ``None`` when the
        contract surface is not configured)."""
        if self._contract_server is not None:
            return self._contract_server
        port = getattr(self.cfg, "contract_port", None)
        if port is None:
            return None
        bind_host = getattr(self.cfg, "contract_bind_host", None) or "0.0.0.0"
        # The action POST is key-gated with the same deployment token the
        # app registers with. When neither the config key nor the env var
        # is present (bare dev runs) the surface stays open — compose
        # deployments always set OPENNVR_INTERNAL_API_KEY.
        action_token = getattr(self.cfg, "opennvr_token", None) or os.environ.get(
            "OPENNVR_INTERNAL_API_KEY"
        )
        server = ContractServer(
            health=self.health_snapshot,
            manifest=self.manifest_snapshot,
            state=self.state_snapshot,
            action=self._dispatch_action,
            action_token=action_token,
            # Apps opt into the /ui surface by defining ui_html() -> str.
            ui=getattr(self, "ui_html", None),
            # X-OpenNVR-User (who is viewing / acting) is signed with the
            # sha256 of our app key; resolved per request so a key
            # issued after start-up is honoured.
            user_secret=lambda: signing_secret(self.credentials.app_key),
            app_id=getattr(self.manifest, "id", None) if self.manifest else None,
            host=bind_host,
            port=int(port),
        )
        server.start()
        self._contract_server = server
        logger.info(
            "contract server listening on %s:%d (/health /manifest /state)",
            bind_host,
            server.port,
        )
        return server

    def stop_contract_server(self) -> None:
        server = self._contract_server
        self._contract_server = None
        if server is not None:
            server.stop()

    def register_with_opennvr(self) -> bool:
        """POST this app's URL + manifest to the OpenNVR app registry.

        Best-effort by contract: every failure path (no registry
        configured, registry down, 4xx/5xx) logs and returns ``False``
        — self-registration must never take the app down. Returns
        ``True`` only on a 2xx from the registry."""
        opennvr_url = getattr(self.cfg, "opennvr_url", None)
        if not opennvr_url:
            return False

        port = getattr(self.cfg, "contract_port", None)
        if self._contract_server is not None:
            # Resolves contract_port=0 to the actually-bound port.
            port = self._contract_server.port
        if port is None:
            logger.warning(
                "self-registration skipped: 'opennvr_url' is set but "
                "'contract_port' is not — the registry needs a contract "
                "endpoint to poll"
            )
            return False

        host = getattr(self.cfg, "contract_host", None) or socket.gethostname()
        creds = self.credentials
        payload = {
            "url": f"http://{host}:{int(port)}",
            "manifest": self.manifest_snapshot(),
            "sdk_version": _sdk_version,
            # No key of our own yet: register with the site key and ask
            # core to mint one (returned once, then persisted).
            "wants_key": not creds.has_app_key,
        }
        # Both header shapes: one value works as an app key, the site's
        # INTERNAL_API_KEY, or a user JWT — see credentials.py.
        headers: dict[str, str] = creds.headers()
        endpoint = f"{str(opennvr_url).rstrip('/')}/api/v1/apps/register"
        try:
            response = httpx.post(
                endpoint,
                json=payload,
                headers=headers,
                timeout=REGISTER_TIMEOUT_SECONDS,
                trust_env=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "app self-registration failed (%s): %s — continuing "
                "without the registry",
                endpoint,
                exc,
            )
            return False
        if response.status_code >= 400:
            logger.warning(
                "app self-registration rejected (%s): HTTP %d: %s",
                endpoint,
                response.status_code,
                response.text[:200],
            )
            if response.status_code == 401 and creds.has_app_key:
                # Our key was rotated or revoked: drop it so the next
                # attempt bootstraps with the site key and asks anew.
                creds.invalidate()
            return False
        logger.info(
            "registered with OpenNVR app registry: %s as %s",
            endpoint,
            payload["url"],
        )
        self._absorb_registration(response)
        return True

    @property
    def credentials(self) -> AppCredentials:
        """The app's credential (app key, else site key) — shared by the
        register call, the config poll and any client built from it."""
        creds = getattr(self, "_credentials", None)
        if creds is None:
            creds = AppCredentials(getattr(self.cfg, "opennvr_token", None))
            self._credentials = creds
        return creds

    def _absorb_registration(self, response) -> None:
        """Take what the registry hands back: an issued app key (once)
        and the compatibility line."""
        try:
            body = response.json()
        except Exception:  # noqa: BLE001
            return
        if not isinstance(body, dict):
            return
        key = body.get("api_key")
        if isinstance(key, str) and key:
            self.credentials.adopt(key)
        registry = body.get("registry") or {}
        min_sdk = registry.get("min_sdk_version") if isinstance(registry, dict) else None
        if min_sdk and _version_tuple(_sdk_version) < _version_tuple(str(min_sdk)):
            logger.warning(
                "OpenNVR %s requires opennvr-app-sdk >= %s (this app runs %s) — "
                "upgrade the SDK; some registry features will not work",
                registry.get("server_version", "?"), min_sdk, _sdk_version)

    # ── Live config delivery (registry poll, spec §05) ─────────────

    def on_config_update(self, config: dict[str, Any]) -> None:
        """Override to apply registry config edits LIVE.

        Called from the poll thread — first on the initial successful
        fetch (which usually re-delivers what the boot config already
        set, so implementations must be IDEMPOTENT), then on every
        change. Applying state must be thread-safe against the app's
        run loop; for typical param swaps, rebuilding into a fresh
        object and rebinding one attribute is atomic enough under the
        GIL (see the license-plate-recognition watchlists for the
        pattern).

        The default logs once that live-reload isn't handled — a
        restart applies the change — so apps that never override still
        behave sanely.
        """
        if not self._config_update_warned:
            self._config_update_warned = True
            logger.info(
                "registry config changed but %s has no live-reload "
                "handler (on_config_update not overridden) — restart "
                "the app to apply",
                type(self).__name__,
            )

    def _config_poll_target(self) -> tuple[str, dict[str, str]] | None:
        """(url, headers) for the config poll, or None when unwired."""
        opennvr_url = getattr(self.cfg, "opennvr_url", None)
        app_id = getattr(self.manifest, "id", None) if self.manifest else None
        if not opennvr_url or not app_id:
            return None
        url = (
            f"{str(opennvr_url).rstrip('/')}/api/v1/apps/{app_id}/config"
        )
        # Headers are re-read on every tick (see _config_poll_once) so a
        # key issued after the poll started is picked up.
        return url, self.credentials.headers()

    def _config_poll_once(self, url: str, headers: dict[str, str]) -> None:
        """One poll tick. Never raises — every failure is a debug log
        (the registry being down must not spam a healthy app's logs)."""
        try:
            response = httpx.get(
                url,
                headers=self.credentials.headers() or headers,
                timeout=CONFIG_POLL_TIMEOUT_SECONDS,
                trust_env=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("config poll failed (%s): %s", url, exc)
            return
        if response.status_code >= 400:
            # 404 is normal before first registration succeeds.
            logger.debug(
                "config poll rejected (%s): HTTP %d", url, response.status_code
            )
            return
        try:
            config = response.json().get("config")
        except Exception:  # noqa: BLE001
            logger.debug("config poll: non-JSON body from %s", url)
            return
        if not isinstance(config, dict):
            return
        if config == self._applied_config:
            return
        self._applied_config = config
        try:
            self.on_config_update(dict(config))
        except Exception:
            # A raising hook must not kill the poll thread — next edit
            # still gets delivered.
            logger.exception("on_config_update raised; config NOT applied")

    def start_config_poll(self) -> bool:
        """Start the live-config poll thread iff wired + enabled.

        Requires ``cfg.opennvr_url`` + a manifest id (same conditions
        as self-registration) and a positive ``config_poll_seconds``
        (default 10s; set ``0`` to disable). Idempotent. Returns True
        when the thread is running.
        """
        if self._config_poll_thread is not None:
            return True
        target = self._config_poll_target()
        if target is None:
            return False
        raw = getattr(self.cfg, "config_poll_seconds", None)
        interval = (
            CONFIG_POLL_DEFAULT_SECONDS if raw is None else float(raw)
        )
        if interval <= 0:
            return False
        url, headers = target

        def _loop() -> None:
            while not self._config_poll_stop.wait(timeout=interval):
                self._config_poll_once(url, headers)

        self._config_poll_stop.clear()
        thread = threading.Thread(
            target=_loop, name="opennvr-app-config-poll", daemon=True
        )
        self._config_poll_thread = thread
        thread.start()
        logger.info(
            "live config delivery: polling %s every %.0fs", url, interval
        )
        return True

    def stop_config_poll(self) -> None:
        thread = self._config_poll_thread
        self._config_poll_thread = None
        self._config_poll_stop.set()
        if thread is not None:
            thread.join(timeout=3.0)


__all__ = [
    "ContractServer",
    "ContractMixin",
    "REGISTER_TIMEOUT_SECONDS",
    "CONFIG_POLL_DEFAULT_SECONDS",
]


def _version_tuple(text: str) -> tuple[int, ...]:
    """``"0.2.0"`` → ``(0, 2, 0)``; non-numeric tails are ignored."""
    out: list[int] = []
    for part in str(text).split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        out.append(int(digits))
    return tuple(out)
