from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "contracts" / "tool-schemas" / "tools.json"


def _contract() -> dict:
    return json.loads(CONTRACT.read_text(encoding="utf-8"))


def test_read_artifact_is_the_only_additive_artifact_tool() -> None:
    tools = {tool["name"]: tool for tool in _contract()["tools"]}
    artifact_tools = sorted(name for name in tools if "artifact" in name)

    assert artifact_tools == ["read_artifact"]
    tool = tools["read_artifact"]
    properties = tool["inputSchema"]["properties"]
    assert tool["inputSchema"]["required"] == ["artifact_id"]
    assert properties["artifact_id"]["type"] == "string"
    assert properties["cursor"]["default"] is None
    assert properties["cursor"].get("anyOf", properties["cursor"].get("type")) in (
        [{"type": "string"}, {"type": "null"}],
        ["string", "null"],
    )
    assert properties["max_bytes"]["default"] == 65_536
    assert properties["max_bytes"]["minimum"] == 1
    assert properties["max_bytes"]["maximum"] == 262_144
    assert set(properties) == {"artifact_id", "cursor", "max_bytes"}
    assert tool["annotations"] == {
        "title": "Read Artifact",
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": False,
    }


def test_bounded_output_result_schemas_are_exact_and_additive() -> None:
    schemas = _contract()["resultSchemas"]
    receipt = schemas["boundedOutputReceiptV1"]
    continuation = schemas["artifactContinuationV1"]
    result = schemas["readArtifactResultV1"]

    assert receipt["required"] == [
        "schema_version", "status", "completeness", "reason", "returned",
    ]
    assert receipt["properties"]["schema_version"]["const"] == 1
    assert receipt["properties"]["status"]["enum"] == ["completed", "partial", "failed"]
    assert receipt["properties"]["completeness"]["enum"] == ["complete", "partial", "unknown"]
    assert "source_active" in receipt["properties"]["reason"]["enum"]

    assert continuation["required"] == ["tool", "arguments"]
    assert continuation["properties"]["tool"]["const"] == "read_artifact"
    assert continuation["properties"]["arguments"]["required"] == ["artifact_id"]

    assert result["required"] == [
        "ok", "artifact_id", "payload", "byte_range", "has_more",
        "eof", "next_cursor", "metadata", "receipt",
    ]
    assert result["properties"]["payload"]["properties"]["type"]["enum"] == ["text", "records", "base64"]
    record = result["properties"]["payload"]["properties"]["records"]["items"]
    assert record["required"] == ["stream", "data"]
    assert result["properties"]["byte_range"]["required"] == ["start", "end"]
    assert result["properties"]["metadata"]["required"] == [
        "kind", "mime_type", "size_bytes", "sha256", "created_at", "expires_at", "ordering",
    ]
    assert result["properties"]["metadata"]["properties"]["sha256"]["type"] == ["string", "null"]


def test_public_catalog_is_v3_with_structured_outputs_for_every_tool() -> None:
    contract = _contract()

    assert contract["contractVersion"] == 3
    assert contract["server"]["toolCount"] == 98
    assert len(contract["tools"]) == 98
    assert all(tool.get("outputSchema", {}).get("type") == "object" for tool in contract["tools"])
