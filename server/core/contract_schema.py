# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""A small JSON-Schema subset for the HA contract (HA-115).

``server/contract/contract.json`` describes payloads with ``type`` (one or a
list; ``integer`` also accepts nothing but ints, ``number`` ints and floats),
``required``, ``properties``, ``items``, ``enum`` and ``$ref`` (a name in the
same ``schemas`` map). Extra fields are always allowed: the contract is
additive. This keeps the check dependency-free.
"""

from __future__ import annotations

from typing import Any

_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool,
    "null": type(None),
}


def _is(value: Any, t: str) -> bool:
    if t == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[t])


def errors(value: Any, schema: dict, schemas: dict, where: str = "$") -> list[str]:
    """Every way ``value`` breaks ``schema``; empty when it conforms."""
    if "$ref" in schema:
        return errors(value, schemas[schema["$ref"]], schemas, where)
    out: list[str] = []
    types = schema.get("type")
    if types is not None:
        types = [types] if isinstance(types, str) else types
        if not any(_is(value, t) for t in types):
            return [f"{where}: expected {'/'.join(types)}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where}: missing {key!r}")
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                out += errors(value[key], sub, schemas, f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += errors(item, schema["items"], schemas, f"{where}[{i}]")
    return out
