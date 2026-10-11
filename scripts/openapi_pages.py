"""Writes docs/openapi-reference/operations.md from docs/openapi.json.

Run it after replacing docs/openapi.json with a newer copy of the server's spec:

    python scripts/openapi_pages.py

The page is generated so it can't drift from the spec. The SDK method names
come from pgpcbot._spec.OPERATIONS (the operationId in snake_case).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "openapi.json"
OUT = ROOT / "docs" / "openapi-reference" / "operations.md"
METHODS = ("get", "post", "put", "patch", "delete")


def snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def cell(text: Any) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")


class Spec:
    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data

    def deref(self, node: Any) -> Any:
        while isinstance(node, dict) and "$ref" in node:
            target: Any = self.data
            for part in node["$ref"].lstrip("#/").split("/"):
                target = target[part]
            node = target
        return node

    def type_of(self, schema: Any) -> str:
        schema = self.deref(schema)
        if not isinstance(schema, dict):
            return ""
        for key in ("oneOf", "anyOf"):
            if key in schema:
                return " or ".join(self.type_of(s) for s in schema[key])
        kind = schema.get("type")
        if isinstance(kind, list):
            kind = " or ".join(kind)
        if kind == "array":
            return f"array of {self.type_of(schema.get('items', {}))}"
        if kind is None and "properties" in schema:
            kind = "object"
        return kind or ""

    def rules(self, schema: Any) -> str:
        schema = self.deref(schema)
        if not isinstance(schema, dict):
            return ""
        out = []
        for key, label in (
            ("enum", "one of"),
            ("format", "format"),
            ("minimum", "min"),
            ("maximum", "max"),
            ("minLength", "min length"),
            ("maxLength", "max length"),
            ("pattern", "pattern"),
            ("default", "default"),
        ):
            if key not in schema:
                continue
            value = schema[key]
            if key == "enum":
                shown = ", ".join(f"`{v}`" for v in value)
            elif isinstance(value, str):
                shown = f"`{value}`"
            else:
                shown = f"`{json.dumps(value)}`"
            out.append(f"{label} {shown}")
        return "; ".join(out)


def _operation(
    spec: Spec, path: str, item: dict[str, Any], method: str, op: dict[str, Any], known: set[str]
) -> list[str]:
    op_id = op.get("operationId", "")
    sdk = snake(op_id)
    if sdk not in known:
        raise SystemExit(f"{op_id}: no SDK method {sdk} in pgpcbot._spec.OPERATIONS")
    lines = [
        f"## `{method.upper()} {path}`",
        "",
        f"**{cell(op.get('summary'))}**",
        "",
        "| | |",
        "|---|---|",
        f"| operationId | `{op_id}` |",
        f"| Tag | {', '.join(op.get('tags', []))} |",
        f"| SDK | {{py:meth}}`~pgpcbot.AsyncClient.{sdk}` |",
        "",
    ]
    if op.get("description"):
        lines += [op["description"].strip(), ""]
    params = [spec.deref(p) for p in item.get("parameters", []) + op.get("parameters", [])]
    if params:
        lines += ["| Parameter | In | Type | Required | Rules | Description |", "|---|---|---|---|---|---|"]
        for p in params:
            schema = p.get("schema", {})
            lines.append(
                f"| `{p['name']}` | {p['in']} | {spec.type_of(schema)} | {'yes' if p.get('required') else 'no'} "
                f"| {cell(spec.rules(schema))} | {cell(p.get('description'))} |"
            )
        lines.append("")
    body = op.get("requestBody")
    if body:
        body = spec.deref(body)
        schema = spec.deref(body.get("content", {}).get("application/json", {}).get("schema", {}))
        required = set(schema.get("required", []))
        lines += [
            f"Request body (JSON{', required' if body.get('required') else ''}):",
            "",
            "| Field | Type | Required | Rules | Description |",
            "|---|---|---|---|---|",
        ]
        for name, prop in schema.get("properties", {}).items():
            lines.append(
                f"| `{name}` | {spec.type_of(prop)} | {'yes' if name in required else 'no'} "
                f"| {cell(spec.rules(prop))} | {cell(spec.deref(prop).get('description'))} |"
            )
        lines.append("")
    lines += ["| Answer | Meaning |", "|---|---|"]
    for status, response in op.get("responses", {}).items():
        lines.append(f"| `{status}` | {cell(spec.deref(response).get('description'))} |")
    lines.append("")
    return lines


def render(spec: Spec, known: set[str]) -> str:
    data = spec.data
    count = sum(1 for item in data["paths"].values() for m in item if m in METHODS)
    lines = [
        "# Every operation",
        "",
        "<!-- generated by scripts/openapi_pages.py from docs/openapi.json: don't edit by hand -->",
        "",
        (
            f"The {count} operations of {data['info']['title']} {data['info']['version']}, as the "
            "server's OpenAPI document describes them. All paths are under `{apiUrl}/bot/v1`. Every "
            "call needs the header `Authorization: Bearer <token>`, and every answer is the envelope "
            '`{"ok": true, "result": ...}` or `{"ok": false, "error": {...}}`.'
        ),
        "",
        (
            "The SDK column links to the method that runs the operation. It has the same name on "
            "`AsyncClient`, `Client`, `Bot` and `SyncBot`."
        ),
        "",
    ]
    for path, item in data["paths"].items():
        for method, op in item.items():
            if method in METHODS:
                lines += _operation(spec, path, item, method, op, known)
    for name, hook in data.get("webhooks", {}).items():
        post = hook.get("post", {})
        lines += [
            f"## Webhook: `{name}`",
            "",
            f"**{cell(post.get('summary'))}**",
            "",
            post.get("description", "").strip(),
            "",
            "The body is one `Update`. In the SDK: {py:meth}`~pgpcbot.Bot.create_webhook_app`.",
            "",
            "| Answer | Meaning |",
            "|---|---|",
        ]
        for status, response in post.get("responses", {}).items():
            lines.append(f"| `{status}` | {cell(spec.deref(response).get('description'))} |")
        lines.append("")
    return "\n".join(lines)


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from pgpcbot._spec import OPERATIONS

    spec = Spec(json.loads(SPEC.read_text(encoding="utf-8")))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(spec, set(OPERATIONS)), encoding="utf-8", newline="\n")
    print(f"wrote {OUT.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
