# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""`ContractServer` — the HTTP surface every app serves, standalone.

Demonstrates: `ContractServer`, `.start`, `.port`, `.stop`,
`contract_openapi`, `contract_asyncapi`, `CONTRACT_API_VERSION`.

Every archetype starts one of these for you when `cfg.contract_port` is
set — you never construct it by hand in an app. It is public because
two other things need it: a process that hosts several apps at once
(the camera agent does this), and a test that wants to hit the real
surface rather than call the snapshot methods directly.

The surface, in full:

    GET  /health            liveness + pipeline vitals
    GET  /manifest          the declarative identity
    GET  /state             live standing state
    GET  /ui                the embedded dashboard (opt-in)
    GET  /openapi.json      OpenAPI 3.1, generated from the manifest
    GET  /asyncapi.json     AsyncAPI 3.0, the bus surface
    POST /actions/{name}    operator verbs (user-JWT only, via core)
    POST /entitlement/verify licence check (opt-in)
"""
from typing import Any

from opennvr_app_sdk import (
    Action, AppManifest, ContractServer, Param, contract_asyncapi, contract_openapi,
)

MANIFEST = AppManifest(
    id="standalone", name="Standalone", version="1.0.0", category="analytics",
    summary="An app contract served by hand.",
    actions=[Action(name="ping", label="Ping",
                    params=[Param("message", str, default="hi")])],
)


def serve() -> ContractServer:
    """Everything the server needs is callables — which is what makes it
    hostable by anything, not just by an archetype."""
    state = {"ticks": 0}

    def health() -> dict[str, Any]:
        return {"ready": True, "uptime_s": 1.0, "events_seen": state["ticks"],
                "alerts_fired": 0, "last_event_age_s": None}

    def on_action(name: str, params: dict[str, Any]) -> Any:
        if name == "ping":
            return {"pong": params.get("message", "hi")}
        raise KeyError(name)          # -> 404, the dispatcher's contract

    server = ContractServer(
        health=health,
        manifest=MANIFEST.to_dict,
        state=lambda: dict(state),
        # Generated from the manifest, so they cannot drift from the app.
        openapi=lambda: contract_openapi(MANIFEST),
        asyncapi=lambda: contract_asyncapi(MANIFEST),
        action=on_action,
        # When set, POST /actions/* REQUIRES this key: an arbitrary
        # process on the internal network gets 401 instead of a free verb.
        action_token="the-deployment-internal-key",
        app_id=MANIFEST.id,
        host="0.0.0.0",
        port=0,                       # 0 = ephemeral; read it back below
    )
    server.start()
    return server


def main() -> None:
    server = serve()
    try:
        print(f"contract on http://127.0.0.1:{server.port}/health")
        print(f"spec     on http://127.0.0.1:{server.port}/openapi.json")
        import time
        time.sleep(3600)
    finally:
        server.stop()
