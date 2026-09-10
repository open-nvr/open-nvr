# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0

"""
Machine-readable specs for an app's own surfaces.

OpenNVR's server-side APIs are FastAPI, so they already publish
**OpenAPI 3.1** at ``/openapi.json`` (core, KAI-C, every AI adapter)
with Swagger UI at ``/docs``. The one surface that had no spec was the
one app authors actually implement: the app contract server in
:mod:`~.contract`, which is a stdlib ``http.server`` and so generates
nothing.

This module closes that. It derives both specs from the app's own
:class:`~.manifest.AppManifest`, which means they cannot drift from the
app — a declared action IS a path, a declared param IS a schema, and an
app that declares no licence gate has no ``/entitlement/verify`` path:

* :func:`contract_openapi` — **OpenAPI 3.1** for the HTTP surface the
  app serves (``/health``, ``/manifest``, ``/state``, ``/ui``,
  ``/actions/{name}``, ``/entitlement/verify``).
* :func:`contract_asyncapi` — **AsyncAPI 3.0** for the NATS surface the
  app consumes and produces (the inference broadcast it subscribes to,
  the alert subjects it publishes on, the contracted domain events its
  ``requires_scopes`` grant).

Both are served by the contract server itself at ``GET /openapi.json``
and ``GET /asyncapi.json``, and both are printable from the CLI::

    opennvr-app spec                     # OpenAPI, to stdout
    opennvr-app spec --format asyncapi   # AsyncAPI
    opennvr-app spec -o openapi.json     # to a file

Which means every OpenNVR app self-describes in the two standards its
consumers already have tooling for: an operator can point Swagger UI or
an SDK generator at a running app, and a bus consumer can generate a
typed client from the AsyncAPI document.
"""
from __future__ import annotations

from typing import Any

from .alerts import DEFAULT_ALERT_SUBJECT_PREFIX
from .manifest import Action, AppManifest, Param

#: The app-contract revision these documents describe (spec §03).
CONTRACT_API_VERSION = "1.3"

_JSON = "application/json"

# Param ``type`` → JSON Schema. The catalog's UI-schema types
# (``geometry.polygon``) carry their editor hint in ``x-opennvr-ui`` so
# a generic OpenAPI consumer still sees a usable shape.
_PRIMITIVES: dict[str, dict[str, Any]] = {
    "float": {"type": "number"},
    "int": {"type": "integer"},
    "str": {"type": "string"},
    "bool": {"type": "boolean"},
    "list": {"type": "array", "items": {}},
    "dict": {"type": "object", "additionalProperties": True},
    "tuple": {"type": "array", "items": {}},
}

_POINT = {
    "type": "array",
    "items": {"type": "number"},
    "minItems": 2,
    "maxItems": 2,
    "description": "An [x, y] vertex, normalized to 0–1 of the frame.",
}

_UI_TYPES: dict[str, dict[str, Any]] = {
    "geometry.polygon": {
        "type": "array", "items": _POINT, "minItems": 3,
        "description": "A closed polygon in normalized frame coordinates.",
    },
    "geometry.line": {
        "type": "array", "items": _POINT, "minItems": 2, "maxItems": 2,
        "description": "A tripwire: two vertices in normalized frame coordinates.",
    },
}


def param_schema(param: Param) -> dict[str, Any]:
    """JSON Schema for one :class:`~.manifest.Param`.

    Python types map to their JSON equivalents; the catalog's UI-schema
    types map to the shape they actually carry on the wire plus an
    ``x-opennvr-ui`` hint. Unknown types degrade to ``string`` rather
    than to an untyped blob, so generated clients stay useful."""
    name = param.type.__name__ if isinstance(param.type, type) else str(param.type)
    if name in _UI_TYPES:
        schema = dict(_UI_TYPES[name])
        schema["x-opennvr-ui"] = name
    else:
        schema = dict(_PRIMITIVES.get(name, {"type": "string"}))
    if param.description:
        schema["description"] = param.description
    if param.default is not None:
        schema["default"] = param.default
    if param.suggestions:
        schema["examples"] = [str(s) for s in param.suggestions]
    if param.per_camera:
        # Per-camera params are collected once per camera, so on the
        # wire they are a mapping keyed by camera id.
        schema = {
            "type": "object",
            "additionalProperties": schema,
            "description": (param.description or "")
            + (" " if param.description else "")
            + "Collected per camera; keys are camera ids.",
            "x-opennvr-per-camera": True,
        }
    return schema


def action_body_schema(action: Action) -> dict[str, Any]:
    """JSON Schema for one action's POST body."""
    properties = {p.name: param_schema(p) for p in action.params}
    required = [p.name for p in action.params if p.required]
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    if action.description:
        schema["description"] = action.description
    return schema


def _error(description: str) -> dict[str, Any]:
    return {
        "description": description,
        "content": {_JSON: {"schema": {"$ref": "#/components/schemas/Error"}}},
    }


def _components(manifest: AppManifest) -> dict[str, Any]:
    schemas: dict[str, Any] = {
        "Error": {
            "type": "object",
            "properties": {"error": {"type": "string"}},
            "required": ["error"],
            "description": "Every non-2xx response on this surface.",
        },
        "Health": {
            "type": "object",
            "description": (
                "Liveness plus pipeline vitals. ``last_event_age_s`` is stall "
                "detection: null before the first event, and a value that keeps "
                "growing means the app is up but its input is not."
            ),
            "properties": {
                "ready": {"type": "boolean"},
                "uptime_s": {"type": "number"},
                "events_seen": {"type": "integer"},
                "alerts_fired": {"type": "integer"},
                "last_event_age_s": {"type": ["number", "null"]},
            },
            "required": ["ready", "uptime_s", "events_seen", "alerts_fired"],
        },
        "Manifest": {
            "type": "object",
            "description": (
                "The app's declarative identity — the same document the App "
                "Catalog stores at registration and renders its card, config "
                "form, state views and actions from."
            ),
            "additionalProperties": True,
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "version": {"type": "string"},
                "category": {"type": "string"},
                "summary": {"type": "string"},
                "params": {"type": "array", "items": {"type": "object"}},
                "emits": {"type": "array", "items": {"type": "object"}},
                "actions": {"type": "array", "items": {"type": "object"}},
                "state_schema": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["id", "name", "version", "category"],
        },
        "State": _state_schema(manifest),
    }
    if manifest.entitlement == "license_key":
        schemas["LicenseKey"] = {
            "type": "object",
            "properties": {"license_key": {"type": "string"}},
            "required": ["license_key"],
        }
        schemas["Entitlement"] = {
            "type": "object",
            "description": "The app's own verdict on a key the administrator entered.",
            "properties": {
                "valid": {"type": "boolean"},
                "plan": {"type": "string"},
                "expires_at": {"type": ["string", "null"], "format": "date-time"},
                "message": {"type": "string"},
                "limits": {"type": "object", "additionalProperties": True},
            },
            "required": ["valid"],
        }
    return {
        "schemas": schemas,
        "securitySchemes": {
            "internalKey": {
                "type": "apiKey", "in": "header", "name": "X-Internal-Api-Key",
                "description": (
                    "The deployment's internal key. Core's proxy forwards it; "
                    "an unauthenticated process on the internal network gets 401."
                ),
            },
            "callToken": {
                "type": "apiKey", "in": "header", "name": "X-OpenNVR-Call",
                "description": (
                    "Short-lived token proving the call came through core "
                    "(api_version ≥ 1.3). Paired with X-OpenNVR-User, which "
                    "carries the signed operator identity."
                ),
            },
        },
    }


def _state_schema(manifest: AppManifest) -> dict[str, Any]:
    """``GET /state`` is app-shaped, so the manifest's declared views are
    what documents it: each view's ``path`` is a property a consumer can
    rely on."""
    schema: dict[str, Any] = {
        "type": "object",
        "additionalProperties": True,
        "description": (
            "Live standing state. Shape is the app's own; the manifest's "
            "state_schema declares which paths the catalog renders."
            if not manifest.state_schema else
            "Live standing state. The properties below are the paths the "
            "manifest's state_schema declares; a missing path renders as an "
            "em-dash, never an error."
        ),
    }
    properties: dict[str, Any] = {}
    for view in manifest.state_schema:
        root = (view.path or view.name).split(".", 1)[0]
        if not root or root in properties:
            continue
        properties[root] = {
            "description": f"{view.label} ({view.kind})"
            + (f" — {view.description}" if view.description else ""),
        }
    if properties:
        schema["properties"] = properties
    return schema


def contract_openapi(manifest: AppManifest, *, port: int | None = None) -> dict[str, Any]:
    """The **OpenAPI 3.1** document for this app's contract server.

    Only the paths the app actually serves appear: ``/ui`` when the
    manifest sets ``has_ui`` with ``ui_mode="internal"``, one
    ``/actions/{name}`` path per declared action, and
    ``/entitlement/verify`` only when ``entitlement="license_key"``.

    ``port`` fills the server URL's default when known (the contract
    server passes its bound port)."""
    paths: dict[str, Any] = {
        "/health": {
            "get": {
                "operationId": "getHealth",
                "summary": "Liveness and pipeline vitals",
                "description": (
                    "Polled by core to drive the App Catalog's status dot. "
                    "Cheap and unauthenticated by design — it exposes no app data."
                ),
                "tags": ["contract"],
                "responses": {
                    "200": {
                        "description": "The app is up.",
                        "content": {_JSON: {
                            "schema": {"$ref": "#/components/schemas/Health"}}},
                    },
                    "500": _error("A snapshot callable raised; the app stays up."),
                },
            }
        },
        "/manifest": {
            "get": {
                "operationId": "getManifest",
                "summary": "The app's declarative identity",
                "description": (
                    "What the catalog renders the app's card, config form, "
                    "state views and actions from, with no app-specific UI code."
                ),
                "tags": ["contract"],
                "responses": {
                    "200": {
                        "description": "The manifest.",
                        "content": {_JSON: {
                            "schema": {"$ref": "#/components/schemas/Manifest"}}},
                    },
                },
            }
        },
        "/state": {
            "get": {
                "operationId": "getState",
                "summary": "Live standing state",
                "description": (
                    "Whatever the app chooses to expose from "
                    "``ContractMixin.state_snapshot``. Read-only, and cheap: "
                    "it is called from the contract server's thread."
                ),
                "tags": ["contract"],
                "responses": {
                    "200": {
                        "description": "The app's live state.",
                        "content": {_JSON: {
                            "schema": {"$ref": "#/components/schemas/State"}}},
                    },
                    "500": _error("state_snapshot raised."),
                },
            }
        },
    }

    if manifest.has_ui and manifest.ui_mode == "internal":
        paths["/ui"] = {
            "get": {
                "operationId": "getUi",
                "summary": "The app's embedded dashboard",
                "description": (
                    "HTML, proxied by core at /api/v1/apps/{id}/ui and rendered "
                    "sandboxed in the catalog."
                ),
                "tags": ["contract"],
                "responses": {
                    "200": {
                        "description": "An HTML fragment or document.",
                        "content": {"text/html": {"schema": {"type": "string"}}},
                    },
                    "500": _error("The UI callable raised."),
                },
            }
        }

    for action in manifest.actions:
        paths[f"/actions/{action.name}"] = {
            "post": {
                "operationId": f"action{_pascal(action.name)}",
                "summary": action.label or action.name,
                "description": (action.description or "")
                + ("\n\nThe catalog asks for confirmation before invoking this."
                   if action.confirm else "")
                + (
                    "\n\nReached only through core's `POST "
                    "/api/v1/apps/{id}/actions/" + action.name + "`, which is "
                    "**user-JWT only**: actions are operator verbs, and the "
                    "OpenNVR Agent's service key can never invoke one."
                ),
                "tags": ["actions"],
                "security": [{"internalKey": [], "callToken": []}],
                "requestBody": {
                    "required": bool(action.params),
                    "content": {_JSON: {"schema": action_body_schema(action)}},
                },
                "responses": {
                    "200": {
                        "description": "The action's result.",
                        "content": {_JSON: {"schema": {
                            "type": "object", "additionalProperties": True}}},
                    },
                    "400": _error("Malformed body, or the app rejected the params."),
                    "401": _error("Missing or bad X-OpenNVR-Call / internal key."),
                    "404": _error("The app does not handle this action."),
                    "413": _error("Body over the 8 MB cap."),
                    "500": _error("The action raised."),
                },
            }
        }

    if manifest.entitlement == "license_key":
        paths["/entitlement/verify"] = {
            "post": {
                "operationId": "verifyEntitlement",
                "summary": "Verify a licence key",
                "description": (
                    "Core asks the app whether a key the administrator entered "
                    "is valid. The verdict is the app's — core stores the key "
                    "encrypted and refuses to enable the app until the app says "
                    "yes. OpenNVR takes no part in the transaction."
                ),
                "tags": ["contract"],
                "security": [{"internalKey": []}],
                "requestBody": {
                    "required": True,
                    "content": {_JSON: {"schema": {
                        "$ref": "#/components/schemas/LicenseKey"}}},
                },
                "responses": {
                    "200": {
                        "description": "The verdict.",
                        "content": {_JSON: {"schema": {
                            "$ref": "#/components/schemas/Entitlement"}}},
                    },
                    "400": _error("Malformed body."),
                    "401": _error("Missing or bad internal key."),
                    "404": _error("This app declares no licence verifier."),
                },
            }
        }

    server: dict[str, Any] = {
        "url": "http://{host}:{port}",
        "description": "The app's contract port on the deployment's internal network.",
        "variables": {
            "host": {"default": manifest.id,
                     "description": "The app's hostname on the compose network."},
            "port": {"default": str(port or 9000),
                     "description": "cfg.contract_port."},
        },
    }

    return {
        "openapi": "3.1.0",
        "info": {
            "title": f"{manifest.name} — app contract",
            "version": manifest.version,
            "summary": manifest.summary or None,
            "description": (
                f"The HTTP surface **{manifest.name}** serves as an OpenNVR app "
                f"(app contract v{CONTRACT_API_VERSION}). Core polls `/health`, "
                "reads `/manifest` at registration, renders `/state` through the "
                "manifest's declared views, and proxies `/actions/*` for "
                "operators.\n\nThis document is generated from the app's own "
                "manifest by `opennvr_app_sdk.openapi`, so it cannot drift from "
                "the app."
            ),
            "license": ({"name": manifest.license} if manifest.license else None),
            "contact": ({"name": manifest.author or manifest.name,
                         "url": manifest.website or None}
                        if (manifest.author or manifest.website) else None),
            "x-opennvr-app-id": manifest.id,
            "x-opennvr-contract-version": CONTRACT_API_VERSION,
        },
        "servers": [server],
        "tags": [
            {"name": "contract",
             "description": "The endpoints every OpenNVR app serves."},
            {"name": "actions",
             "description": "Operator verbs this app declares in its manifest."},
        ],
        "paths": paths,
        "components": _components(manifest),
    }


# ── AsyncAPI — the bus surface ──────────────────────────────────────


def contract_asyncapi(manifest: AppManifest) -> dict[str, Any]:
    """The **AsyncAPI 3.0** document for this app's NATS surface.

    Three groups of channels, all derived from the manifest: what the
    app subscribes to (``subscribes``), what it publishes
    (``opennvr.alerts.app.<id>.<camera_id>``, one per declared alert
    type), and the contracted domain events its ``requires_scopes``
    grant — scopes are how an app asks for PII-bearing events, so they
    belong in the spec rather than in prose."""
    channels: dict[str, Any] = {}
    operations: dict[str, Any] = {}

    if manifest.subscribes:
        key, channel, summary = _subscribe_channel(manifest.subscribes)
        channels[key] = channel
        operations["receive" + _pascal(key)] = {
            "action": "receive",
            "channel": {"$ref": f"#/channels/{key}"},
            "summary": summary,
        }

    if manifest.emits:
        channels["alerts"] = {
            "address": f"{DEFAULT_ALERT_SUBJECT_PREFIX}.app.{manifest.id}.{{camera_id}}",
            "title": "Alerts this app fires",
            "description": (
                "The §11.5 alert envelope. Subject segments mirror the alert's "
                "source block, so `opennvr.alerts.app.>` is every app-emitted "
                f"alert and `{DEFAULT_ALERT_SUBJECT_PREFIX}.app.{manifest.id}.>` "
                "is this app's."
            ),
            "parameters": {"camera_id": {
                "description": "The camera the alert is about."}},
            "messages": {"alert": {"$ref": "#/components/messages/Alert"}},
        }
        operations["sendAlerts"] = {
            "action": "send",
            "channel": {"$ref": "#/channels/alerts"},
            "summary": "Fire an operator-visible alert.",
            "description": "Declared alert types: "
                           + ", ".join(f"{a.name} ({a.severity})" for a in manifest.emits),
        }

    for scope in manifest.requires_scopes:
        if ":" not in scope:
            continue
        name = scope.split(":", 1)[1]
        key = "event" + _pascal(name)
        if key in channels or any(
            c.get("address") == f"opennvr.events.{name}.v1.>" for c in channels.values()
        ):
            # Already covered by the subscribe channel — a domain-event
            # consumer names the same subject in both places.
            continue
        channels[key] = {
            "address": f"opennvr.events.{name}.v1.{{camera_id}}",
            "title": f"{name} (contracted domain event)",
            "description": (
                f"Granted by the `{scope}` scope. Domain events are versioned "
                "in the subject and defined in EVENT_CONTRACTS.md; consuming a "
                "PII-bearing one is a declared, granted and audited capability."
            ),
            "parameters": {"camera_id": {"description": "The camera."}},
            "messages": {"domainEvent": {
                "$ref": "#/components/messages/DomainEvent"}},
        }
        operations["receive" + _pascal(name)] = {
            "action": "receive",
            "channel": {"$ref": f"#/channels/{key}"},
            "summary": f"Consume {name} events.",
        }

    return {
        "asyncapi": "3.0.0",
        "info": {
            "title": f"{manifest.name} — bus surface",
            "version": manifest.version,
            "description": (
                f"What **{manifest.name}** consumes from and publishes to the "
                "OpenNVR event bus. Generated from the app's manifest by "
                "`opennvr_app_sdk.openapi`; the normative definitions of the "
                "envelopes are EVENT_CONTRACTS.md (domain events) and §11.5 "
                "(alerts)."
            ),
            "license": ({"name": manifest.license} if manifest.license else None),
        },
        "servers": {"bus": {
            "host": "nats:4222",
            "protocol": "nats",
            "description": (
                "The deployment's NATS event bus. Apps connect with their own "
                "credential to the apps-facing server, never with the site key."
            ),
        }},
        "channels": channels,
        "operations": operations,
        "components": {"messages": {
            "InferenceCompleted": {
                "name": "InferenceCompletedEvent",
                "title": "One completed inference",
                "contentType": _JSON,
                "payload": {
                    "type": "object",
                    "properties": {
                        "correlation_id": {"type": "string"},
                        "adapter": {"type": "string"},
                        "adapter_version": {"type": "string"},
                        "camera_id": {"type": "string"},
                        "model_fingerprint": {"type": "string"},
                        "completed_at": {"type": "string", "format": "date-time"},
                        "result": {
                            "type": "object",
                            "properties": {"detections": {
                                "type": "array",
                                "items": {"$ref": "#/components/schemas/Detection"},
                            }},
                        },
                    },
                    "required": ["camera_id", "result"],
                },
            },
            "Alert": {
                "name": "Alert",
                "title": "An app-emitted alert (§11.5)",
                "contentType": _JSON,
                "payload": {"$ref": "#/components/schemas/Alert"},
            },
            "DomainEvent": {
                "name": "DomainEvent",
                "title": "A contracted domain event",
                "contentType": _JSON,
                "payload": {"$ref": "#/components/schemas/DomainEvent"},
            },
        }, "schemas": {
            "Detection": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "track_id": {"type": ["string", "null"]},
                    "bbox": {
                        "type": "object",
                        "description": "NormalizedBBox — x/y/w/h in 0–1 of the frame.",
                        "properties": {
                            "x": {"type": "number"}, "y": {"type": "number"},
                            "w": {"type": "number"}, "h": {"type": "number"},
                        },
                    },
                },
                "required": ["label"],
            },
            "Alert": {
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string"},
                    "fired_at": {"type": "string", "format": "date-time"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "severity": {"enum": ["low", "medium", "high", "critical"]},
                    "source": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": ["app", "adapter", "kai-c"]},
                            "name": {"type": "string"},
                            "version": {"type": "string"},
                        },
                    },
                    "camera_id": {"type": "string"},
                    "correlation_id": {"type": ["string", "null"]},
                    "evidence": {"type": "object", "additionalProperties": True},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["alert_id", "fired_at", "title", "camera_id", "severity"],
            },
            "DomainEvent": {
                "type": "object",
                "description": "The EVENT_CONTRACTS.md envelope.",
                "properties": {
                    "id": {"type": "string"},
                    "schema": {"type": "string"},
                    "correlation_id": {"type": ["string", "null"]},
                    "camera_id": {"type": "string"},
                    "ts": {"type": "string", "format": "date-time"},
                    "producer": {"type": "string"},
                    "payload": {"type": "object", "additionalProperties": True},
                },
                "required": ["id", "schema", "camera_id", "ts", "producer", "payload"],
            },
        }},
    }


def _subscribe_channel(address: str) -> tuple[str, dict[str, Any], str]:
    """Classify what an app subscribes to by its subject tree, so the
    channel carries the right envelope. A ``Detector`` on
    ``opennvr.inference.>`` and a ``DomainEventSubscriber`` on
    ``opennvr.events.plate.recognized.v1.>`` are both "subscribes" in the
    manifest but are different messages on the wire."""
    if address.startswith("opennvr.events."):
        return "domainEvents", {
            "address": address,
            "title": "Contracted domain events",
            "description": (
                "Versioned domain events from the `opennvr.events.*` tree, "
                "defined in EVENT_CONTRACTS.md. The major version is part of "
                "the subject, so a v2 can run beside v1 during migration."
            ),
            "messages": {"domainEvent": {
                "$ref": "#/components/messages/DomainEvent"}},
        }, "Consume contracted domain events."
    if address.startswith("opennvr.alerts."):
        return "alertStream", {
            "address": address,
            "title": "The alert bus",
            "description": (
                "Alerts other apps, adapters and KAI-C fire (§11.5). The "
                "AlertSubscriber archetype relays these onward — to Home "
                "Assistant, a SIEM, a notifier."
            ),
            "messages": {"alert": {"$ref": "#/components/messages/Alert"}},
        }, "Consume alerts fired elsewhere in the deployment."
    return "inference", {
        "address": address,
        "title": "Inference broadcast",
        "description": (
            "KAI-C publishes one InferenceCompletedEvent per completed "
            "inference. Adapter GPU is paid once; every subscriber fans out "
            "from the same stream."
        ),
        "messages": {"inferenceCompleted": {
            "$ref": "#/components/messages/InferenceCompleted"}},
    }, "Consume detections another app is already driving."


# ── Helpers ─────────────────────────────────────────────────────────


def prune(value: Any) -> Any:
    """Drop ``None`` values recursively — OpenAPI and AsyncAPI both
    reject explicit nulls where a field is simply absent."""
    if isinstance(value, dict):
        return {k: prune(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [prune(v) for v in value]
    return value


def _pascal(name: str) -> str:
    parts = name.replace("-", " ").replace("_", " ").replace(".", " ").split()
    return "".join(p[:1].upper() + p[1:] for p in parts)


__all__ = [
    "CONTRACT_API_VERSION",
    "contract_openapi",
    "contract_asyncapi",
    "param_schema",
    "action_body_schema",
    "prune",
]
