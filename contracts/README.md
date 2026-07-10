# Shared MCP contracts

`tool-schemas/tools.json` is the canonical public tool surface shared by the
Python and Go servers. It records tool names, descriptions, JSON input schemas,
and MCP annotations.

Regenerate it through the Python contract exporter with:

```bash
make contracts
```

Both implementations have acceptance tests that compare their live MCP tool
list with this file. Contract changes are therefore intentional, reviewed API
changes rather than incidental differences between languages.
