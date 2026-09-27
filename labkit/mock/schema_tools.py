"""JSON Schema helpers for the mock: synthesize a conforming instance, and validate one.

`synthesize` powers the generic fallback for structured-output requests: whatever
pydantic model a lab passes to `client.messages.parse(output_format=...)`, the mock
can return JSON that validates - mirroring the real API's guarantee that
`output_config.format` responses parse.
"""

from __future__ import annotations

import re
from typing import Any

import jsonschema

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ID = re.compile(r"\b[A-Z]{2,5}-\d{3,6}\b")


def resolve(schema: dict, root: dict) -> dict:
    seen = 0
    while isinstance(schema, dict) and "$ref" in schema and seen < 20:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return {}
        node: Any = root
        for part in ref[2:].split("/"):
            node = node.get(part, {}) if isinstance(node, dict) else {}
        schema = node
        seen += 1
    return schema


def synthesize(schema: dict, *, root: dict | None = None, text: str = "", name: str = "", depth: int = 0) -> Any:
    """Build a small, deterministic instance of `schema` (uses hints from `text`)."""
    root = root or schema
    schema = resolve(schema, root)
    if depth > 8 or not isinstance(schema, dict):
        return None
    if "const" in schema:
        return schema["const"]
    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "default" in schema:
        return schema["default"]
    for key in ("anyOf", "oneOf"):
        if key in schema:
            options = [resolve(o, root) for o in schema[key]]
            non_null = [o for o in options if o.get("type") != "null"]
            return synthesize((non_null or options)[0], root=root, text=text, name=name, depth=depth + 1)
    if "allOf" in schema:
        merged: dict = {}
        for part in schema["allOf"]:
            merged.update(resolve(part, root))
        return synthesize(merged, root=root, text=text, name=name, depth=depth + 1)
    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), "null")
    if kind == "object" or "properties" in schema:
        return {prop: synthesize(sub, root=root, text=text, name=prop, depth=depth + 1)
                for prop, sub in (schema.get("properties") or {}).items()}
    if kind == "array":
        item = synthesize(schema.get("items") or {"type": "string"}, root=root, text=text, name=name, depth=depth + 1)
        return [item]
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "date":
            return "2026-09-15"
        if fmt == "date-time":
            return "2026-09-15T09:00:00Z"
        if fmt == "email" or "email" in name.lower():
            match = _EMAIL.search(text)
            return match.group(0) if match else "customer@example.com"
        if fmt in ("uri", "url"):
            return "https://example.com"
        if fmt == "uuid":
            return "00000000-0000-4000-8000-000000000000"
        if name.lower().endswith("_id") or name.lower() == "id":
            match = _ID.search(text)
            if match:
                return match.group(0)
        return f"mock {name or 'value'}".strip()
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return None


def validation_errors(instance: Any, schema: dict) -> list[str]:
    validator = jsonschema.Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.path) or '<root>'}: {e.message}" for e in validator.iter_errors(instance)]
