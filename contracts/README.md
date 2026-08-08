# Shared MCP contracts

`tool-schemas/tools.json` is the canonical public tool surface shared by the
Python and Go servers. It records tool names, descriptions, JSON input schemas,
JSON output schemas, and MCP annotations. Contract v3 requires every one of the
98 canonical tools to publish a concrete additive `outputSchema`; an untyped
`{"type":"object"}` placeholder is rejected by the generation checks.

Regenerate it through the Python contract exporter with:

```bash
make contracts
```

Both implementations have acceptance tests that compare their live MCP tool
list with this file. Contract changes are therefore intentional, reviewed API
changes rather than incidental differences between languages.
