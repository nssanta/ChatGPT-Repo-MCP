"""Verify shared contract copies and release metadata without modifying files."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import anyio

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python" / "src"))
sys.path.insert(0, str(ROOT / "contracts" / "acceptance"))
os.environ.setdefault("PROJECT_ROOT", str(ROOT))

from bounded_output_contract import BOUNDED_OUTPUT_RESULT_SCHEMAS
from chatrepo_mcp import __version__
from chatrepo_mcp.server import mcp


def _required_branches(schema: dict[str, object]) -> list[set[str]]:
    definitions = schema.get("$defs")
    defs = definitions if isinstance(definitions, dict) else {}

    def walk(
        node: object,
        seen_refs: frozenset[str],
        inherited: frozenset[str] = frozenset(),
    ) -> list[set[str]]:
        if not isinstance(node, dict):
            return []
        local = set(inherited)
        required = node.get("required")
        if isinstance(required, list) and all(isinstance(field, str) for field in required):
            local.update(required)
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in seen_refs or name not in defs:
                return []
            return walk(defs[name], seen_refs | {name}, frozenset(local))
        branches: list[set[str]] = []
        descended = False
        for keyword in ("anyOf", "oneOf", "allOf"):
            alternatives = node.get(keyword)
            if isinstance(alternatives, list):
                for alternative in alternatives:
                    descended = True
                    branches.extend(walk(alternative, seen_refs, frozenset(local)))
        then_schema = node.get("then")
        if isinstance(then_schema, dict):
            descended = True
            branches.extend(walk(then_schema, seen_refs, frozenset(local)))
        if not descended and local:
            branches.append(local)
        return branches

    return walk(schema, frozenset())


def _rejects_empty_object(schema: dict[str, object]) -> bool:
    definitions = schema.get("$defs")
    defs = definitions if isinstance(definitions, dict) else {}

    def walk(node: object, seen_refs: frozenset[str]) -> bool:
        if not isinstance(node, dict):
            return False
        required = node.get("required")
        if isinstance(required, list) and bool(required):
            return True
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in seen_refs or name not in defs:
                return False
            return walk(defs[name], seen_refs | {name})
        all_of = node.get("allOf")
        if isinstance(all_of, list) and any(walk(branch, seen_refs) for branch in all_of):
            return True
        for keyword in ("anyOf", "oneOf"):
            alternatives = node.get(keyword)
            if isinstance(alternatives, list) and alternatives:
                return all(walk(branch, seen_refs) for branch in alternatives)
        return False

    return walk(schema, frozenset())


def _variant_branches_are_additive(schema: dict[str, object]) -> bool:
    definitions = schema.get("$defs")
    defs = definitions if isinstance(definitions, dict) else {}

    def resolve(node: object, seen_refs: frozenset[str]) -> bool:
        if not isinstance(node, dict):
            return False
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            name = reference.removeprefix("#/$defs/")
            if name in seen_refs or name not in defs:
                return False
            return resolve(defs[name], seen_refs | {name})
        return node.get("type") == "object" and node.get("additionalProperties") is True

    alternatives = schema.get("anyOf") or schema.get("oneOf")
    return isinstance(alternatives, list) and bool(alternatives) and all(
        resolve(branch, frozenset()) for branch in alternatives
    )


async def main() -> None:
    canonical_path = ROOT / "contracts" / "tool-schemas" / "tools.json"
    go_path = ROOT / "go" / "internal" / "contracts" / "tools.json"
    canonical_bytes = canonical_path.read_bytes()
    contract = json.loads(canonical_bytes)
    tools = await mcp.list_tools()
    live = [
        tool.model_dump(by_alias=True, exclude_none=True)
        for tool in sorted(tools, key=lambda item: item.name)
    ]
    if contract.get("contractVersion") != 3:
        raise SystemExit("canonical contractVersion must be 3; run `make contracts`")
    if len(live) != 98:
        raise SystemExit(f"canonical public surface must contain exactly 98 tools, got {len(live)}")
    for tool in live:
        schema = tool.get("outputSchema")
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise SystemExit(f"tool {tool['name']!r} is missing an object outputSchema")
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            raise SystemExit(f"tool {tool['name']!r} has a generic object-only outputSchema")
        if schema.get("additionalProperties") is not True:
            raise SystemExit(f"tool {tool['name']!r} outputSchema is not additive")
        error_fields = {"ok", "error", "error_kind"}
        if not error_fields.issubset(properties):
            raise SystemExit(f"tool {tool['name']!r} outputSchema lacks the typed error envelope")
        if not (set(properties) - error_fields):
            raise SystemExit(f"tool {tool['name']!r} outputSchema lacks success-specific fields")
        required_branches = _required_branches(schema)
        if not required_branches or not _rejects_empty_object(schema):
            raise SystemExit(f"tool {tool['name']!r} outputSchema accepts an unconstrained empty object")
        has_typed_error = any({"ok", "error_kind"}.issubset(branch) for branch in required_branches)
        has_legacy_error = any("error" in branch for branch in required_branches)
        if not has_typed_error or not has_legacy_error:
            raise SystemExit(f"tool {tool['name']!r} outputSchema lacks its required error variants")
        if not any(branch - error_fields for branch in required_branches):
            raise SystemExit(f"tool {tool['name']!r} outputSchema lacks required success fields")
        if not _variant_branches_are_additive(schema):
            raise SystemExit(f"tool {tool['name']!r} outputSchema has a non-additive result variant")
    if live != contract["tools"]:
        raise SystemExit(
            "Canonical contract is stale against the full Python live schema; "
            "run `make contracts`"
        )
    if canonical_bytes != go_path.read_bytes():
        raise SystemExit("Go embedded contract is stale; run `make contracts`")
    if contract.get("resultSchemas") != BOUNDED_OUTPUT_RESULT_SCHEMAS:
        raise SystemExit("bounded result schemas differ from the canonical contract; run `make contracts`")

    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    pyproject = (ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version = "([^"]+)"$', pyproject, re.MULTILINE)
    if not match or match.group(1) != version or __version__ != version:
        raise SystemExit("VERSION, Python package version, and __version__ must match")
    if contract["server"]["version"] != version:
        raise SystemExit("contract server version differs from VERSION")
    if contract["server"]["toolCount"] != len(live):
        raise SystemExit("contract toolCount differs from live tool count")
    print(f"contract ok: version={version} tools={len(live)}")


if __name__ == "__main__":
    anyio.run(main)
