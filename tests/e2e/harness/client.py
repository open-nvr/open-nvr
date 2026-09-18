# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``OpenNVRClient`` — the only way a test talks to the API.

Funnelling every call through one object is what makes the rest of the suite
possible. It is where authentication lives (once, not in forty tests), where
entities get registered for teardown (automatically, so nobody forgets), and
where the correlation identity that ties a failure to its logs is attached.

**Correlation.** Every request carries::

    User-Agent: opennvr-e2e/<sandbox-namespace>

and every entity the test creates carries the same namespace in its name, so
one token traces a test across services, database rows and the evidence
bundle. Note that it does *not* find anything in core's own logs — core writes
its request log nowhere retrievable (see ``evidence.py``) — but it does work
for mediamtx, nats and nginx, and the naming half works everywhere.

An inbound ``X-Request-ID`` would be pointless: ``RequestLoggingMiddleware``
mints its own and ignores anything supplied. It returns that id in the
*response* header, so each is captured for the report.

**Auth.** Bearer JWT plus the device token issued at login. The device token is
sent deliberately: ``DeviceFirewallMiddleware`` auto-approves the first browser
to authenticate on a fresh install, and the suite shares that one enrolment
between the API client and Playwright. Minting a second identity would leave it
``pending`` and 403 under enforcement.
"""

from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

import httpx

from . import routes
from .budgets import BUDGETS
from .sandbox import Sandbox

#: Status codes accepted by default. Anything else raises ``ApiError``.
_OK = (200, 201, 202, 204)


class ApiError(AssertionError):
    """An API call returned an unexpected status.

    Subclasses ``AssertionError`` so pytest reports it as a failure rather than
    an internal error: an unexpected 500 from the system under test is a
    finding, not a broken harness.
    """

    def __init__(
        self,
        method: str,
        url: str,
        status: int,
        expected: Iterable[int],
        body: str,
        request_id: str | None,
    ) -> None:
        self.status = status
        self.body = body
        self.request_id = request_id
        expected_text = ", ".join(str(code) for code in expected)
        lines = [
            f"{method} {url}",
            f"  expected : {expected_text}",
            f"  got      : {status}",
            f"  body     : {_truncate(body)}",
        ]
        if request_id:
            lines.append(f"  server request id: {request_id}")
        super().__init__("\n".join(lines))


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + f"… ({len(text)} chars)"


class OpenNVRClient:
    """An authenticated HTTP client bound to one test's sandbox.

    Construct via the ``client`` fixture. The session-scoped credentials are
    shared; the sandbox binding and correlation identity are per test.
    """

    def __init__(
        self,
        base_url: str,
        *,
        api_prefix: str = "/api/v1",
        token: str | None = None,
        device_token: str | None = None,
        internal_key: str | None = None,
        sandbox: Sandbox | None = None,
        verify: bool = False,
        request_id_sink: list[str] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = api_prefix.rstrip("/")
        self.token = token
        self.device_token = device_token
        self.internal_key = internal_key
        self.sandbox = sandbox
        # Server-side request ids, newest last. When the evidence context
        # supplies a list, append straight into it: the failure bundle is
        # built during the *call* phase, before fixture teardown could hand
        # anything over, so collecting these lazily would always be too late.
        self.request_ids: list[str] = request_id_sink if request_id_sink is not None else []

        agent = f"opennvr-e2e/{sandbox.namespace}" if sandbox else "opennvr-e2e"
        self._http = httpx.Client(
            base_url=self.base_url,
            timeout=BUDGETS.REQUEST,
            verify=verify,  # the stack's TLS is self-signed by nginx-certs-init
            follow_redirects=True,
            headers={"User-Agent": agent, "Accept": "application/json"},
        )

    # -- lifecycle -------------------------------------------------------
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "OpenNVRClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- low level -------------------------------------------------------
    def url_for(self, path: str, *, absolute: bool = False) -> str:
        """Join a route to the API prefix. ``absolute=True`` skips the prefix."""
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if absolute:
            return f"{self.base_url}{path}"
        return f"{self.base_url}{self.api_prefix}{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        absolute: bool = False,
        expect: Iterable[int] | None = _OK,
        json_body: Any = None,
        data: Any = None,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        internal: bool = False,
        auth: bool = True,
    ) -> httpx.Response:
        """Issue one request.

        Args:
            expect: accepted status codes; ``None`` accepts anything (use when
                the status *is* the assertion, e.g. checking a 403).
            internal: authenticate with ``X-Internal-Api-Key`` instead of the
                user JWT — for the ``/internal/camera-agent/*`` door.
            auth: set False for the unauthenticated surfaces (login, /health).
        """
        url = self.url_for(path, absolute=absolute)
        merged: dict[str, str] = {}
        if auth:
            if internal:
                if not self.internal_key:
                    raise RuntimeError(
                        "internal=True requires INTERNAL_API_KEY; the client was "
                        "built without one."
                    )
                merged["X-Internal-Api-Key"] = self.internal_key
            elif self.token:
                merged["Authorization"] = f"Bearer {self.token}"
            if self.device_token:
                merged["X-Device-Token"] = self.device_token
        if headers:
            merged.update(headers)

        response = self._http.request(
            method,
            url,
            json=json_body,
            data=data,
            params=params,
            headers=merged,
        )

        server_id = response.headers.get("X-Request-ID")
        if server_id:
            self.request_ids.append(server_id)

        if expect is not None and response.status_code not in expect:
            raise ApiError(
                method.upper(), url, response.status_code, expect, response.text, server_id
            )
        return response

    # Thin verbs. They exist so call sites read as the HTTP they perform.
    def get(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("POST", path, **kw)

    def put(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("PUT", path, **kw)

    def delete(self, path: str, **kw: Any) -> httpx.Response:
        return self.request("DELETE", path, **kw)

    def json(self, path: str, **kw: Any) -> Any:
        """GET and decode. The common read shape."""
        return self.get(path, **kw).json()

    # -- domain helpers: every create registers its own teardown ---------
    #
    # These are the reason a test never writes cleanup code. Add a helper here
    # whenever a new entity type appears, and every future test gets correct
    # teardown for it for free.

    def _require_sandbox(self, what: str) -> Sandbox:
        if self.sandbox is None:
            raise RuntimeError(
                f"{what} needs a sandbox so it can be cleaned up. Use the "
                f"`client` fixture rather than building OpenNVRClient directly."
            )
        return self.sandbox

    def create_camera(
        self,
        *,
        label: str = "cam",
        rtsp_url: str,
        ip_address: str,
        port: int = 8554,
        force: bool = True,
        **extra: Any,
    ) -> dict:
        """Create a camera and register its deletion.

        ``force=True`` by default: the fake-camera rig serves every stream from
        one IP, and the duplicate-IP guard would reject every camera after the
        first.
        """
        sandbox = self._require_sandbox("create_camera")
        payload = {
            "name": sandbox.name(label)[:100],
            "description": f"OpenNVR E2E ({sandbox.test_id})",
            "ip_address": ip_address,
            "port": port,
            "rtsp_url": rtsp_url,
            "location": "e2e",
            **extra,
        }
        camera = self.post(
            routes.CAMERAS, json_body=payload, params={"force": str(force).lower()}
        ).json()
        cam_id = camera["id"]
        sandbox.track(
            f"camera {cam_id} ({payload['name']})",
            lambda: self.delete(routes.CAMERA(cam_id), expect=None),
        )
        return camera

    def create_user(
        self,
        *,
        label: str,
        password: str,
        role_id: int | None = None,
        is_superuser: bool = False,
        **extra: Any,
    ) -> dict:
        """Create a user and register its deletion."""
        sandbox = self._require_sandbox("create_user")
        username = sandbox.name(label)[:50]
        payload = {
            # example.com, not .invalid or .local: the API validates with
            # email-validator, which rejects special-use TLDs outright (422).
            # RFC 2606 reserves example.com exactly so it can never route,
            # which is the property wanted here.
            "username": username,
            "email": f"{username}@example.com",
            "password": password,
            "first_name": "E2E",
            "last_name": label,
            "is_active": True,
            "is_superuser": is_superuser,
            **extra,
        }
        if role_id is not None:
            payload["role_id"] = role_id
        user = self.post(routes.USERS, json_body=payload).json()
        user_id = user["id"]
        sandbox.track(
            f"user {user_id} ({username})",
            lambda: self.delete(routes.USER(user_id), expect=None),
        )
        return user

    def grant_camera_permission(
        self, camera_id: int, user_id: int, **extra: Any
    ) -> dict:
        """Grant a camera permission and register its revocation."""
        sandbox = self._require_sandbox("grant_camera_permission")
        granted = self.post(
            routes.CAMERA_PERMISSIONS(camera_id),
            json_body={"user_id": user_id, **extra},
        ).json()
        sandbox.track(
            f"camera {camera_id} permission for user {user_id}",
            lambda: self.delete(
                routes.CAMERA_PERMISSION(camera_id, user_id), expect=None
            ),
        )
        return granted

    # -- derived clients -------------------------------------------------
    def as_user(self, token: str, device_token: str | None = None) -> "OpenNVRClient":
        """A client for a different principal, sharing this sandbox.

        Used by RBAC tests to act as the operator or viewer they just created.
        Entities the derived client creates are tracked in the same sandbox, so
        teardown still covers them.
        """
        return OpenNVRClient(
            self.base_url,
            api_prefix=self.api_prefix,
            token=token,
            device_token=device_token or self.device_token,
            internal_key=self.internal_key,
            sandbox=self.sandbox,
            verify=False,
        )

    # -- convenience -----------------------------------------------------
    def login(self, username: str, password: str, code: str | None = None) -> dict:
        """Authenticate and return the token payload. Does not mutate self."""
        body: dict[str, Any] = {"username": username, "password": password}
        if code:
            body["code"] = code
        return self.post(
            routes.AUTH_LOGIN_JSON, json_body=body, auth=False, expect=(200,)
        ).json()

    def healthy(self) -> bool:
        """Cheap liveness probe used by the between-test stack gate."""
        try:
            resp = self.get(routes.ABS_HEALTH, absolute=True, expect=None, auth=False)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200


def pretty(payload: Any) -> str:
    """Readable JSON for assertion messages and evidence files."""
    try:
        return json.dumps(payload, indent=2, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(payload)


__all__ = ["OpenNVRClient", "ApiError", "pretty"]
