"""Additive public schemas for bounded tool output.

This module is deliberately runtime-neutral: the Python exporter and the
read-only contract checker consume the same fragment, while both runtimes are
validated against the generated document by their existing parity suites.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

BOUNDED_OUTPUT_RESULT_SCHEMAS: dict[str, Any] = {
    "boundedOutputReceiptV1": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "schema_version": {"type": "integer", "const": 1},
            "status": {"type": "string", "enum": ["completed", "partial", "failed"]},
            "completeness": {"type": "string", "enum": ["complete", "partial", "unknown"]},
            "reason": {
                "type": "string",
                "enum": [
                    "none", "inline_limit", "source_active", "result_limit", "scan_timeout",
                    "policy_filtered", "source_changed", "artifact_quota", "unknown",
                ],
            },
            "requested": {"type": "object", "additionalProperties": True},
            "applied": {"type": "object", "additionalProperties": True},
            "returned": {"type": "object", "additionalProperties": True},
            "total": {"type": ["object", "null"], "additionalProperties": True},
            "warnings": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["schema_version", "status", "completeness", "reason", "returned"],
    },
    "artifactContinuationV1": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "tool": {"type": "string", "const": "read_artifact"},
            "arguments": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "artifact_id": {"type": "string", "minLength": 1},
                    "cursor": {"type": "string", "minLength": 1},
                },
                "required": ["artifact_id"],
            },
        },
        "required": ["tool", "arguments"],
    },
    "readArtifactResultV1": {
        "type": "object",
        "additionalProperties": True,
        "properties": {
            "ok": {"type": "boolean"},
            "artifact_id": {"type": "string", "minLength": 1},
            "payload": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "type": {"type": "string", "enum": ["text", "records", "base64"]},
                    "text": {"type": "string"},
                    "records": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": True,
                            "properties": {
                                "stream": {"type": "string", "enum": ["stdout", "stderr", "combined", "data"]},
                                "data": {"type": "string"},
                            },
                            "required": ["stream", "data"],
                        },
                    },
                    "base64": {"type": "string"},
                },
                "required": ["type"],
            },
            "byte_range": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "start": {"type": "integer", "minimum": 0},
                    "end": {"type": "integer", "minimum": 0},
                },
                "required": ["start", "end"],
            },
            "has_more": {"type": "boolean"},
            "eof": {"type": "boolean"},
            "next_cursor": {"type": ["string", "null"]},
            "metadata": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "kind": {"type": "string"},
                    "mime_type": {"type": "string"},
                    "size_bytes": {"type": "integer", "minimum": 0},
                    "sha256": {"type": ["string", "null"]},
                    "created_at": {"type": "string"},
                    "expires_at": {"type": ["string", "null"]},
                    "ordering": {
                        "type": "string",
                        "enum": ["stdout_then_stderr", "capture_order", "source_order"],
                    },
                },
                "required": [
                    "kind", "mime_type", "size_bytes", "sha256", "created_at",
                    "expires_at", "ordering",
                ],
            },
            "receipt": {"$ref": "#/resultSchemas/boundedOutputReceiptV1"},
        },
        "required": [
            "ok", "artifact_id", "payload", "byte_range", "has_more",
            "eof", "next_cursor", "metadata", "receipt",
        ],
    },
}

def read_artifact_output_schema() -> dict[str, Any]:
    """Return the standalone MCP output schema expected from live registration."""
    schema = deepcopy(BOUNDED_OUTPUT_RESULT_SCHEMAS["readArtifactResultV1"])
    schema["properties"]["receipt"] = {"$ref": "#/$defs/boundedOutputReceiptV1"}
    schema["$defs"] = {
        "boundedOutputReceiptV1": BOUNDED_OUTPUT_RESULT_SCHEMAS["boundedOutputReceiptV1"],
    }
    return schema
