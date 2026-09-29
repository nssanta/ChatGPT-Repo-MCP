# File transfer between ChatGPT and the connected machine

ChatRepo MCP exposes two binary-safe file-transfer tools in both the Python and
Go implementations:

- `receive_chat_file` — ChatGPT attachment -> PC/VPS.
- `export_file_to_chat` — PC/VPS file -> ChatGPT.

The feature is transport-independent. It works through OpenAI Secure MCP Tunnel
and through a normal public HTTPS `/mcp` endpoint. The tunnel is only one way
to make a private MCP server reachable; the transfer protocol itself does not
depend on `tunnel-client`.

A private `http://127.0.0.1:8000/mcp` listener is not reachable from hosted
ChatGPT by itself. For ChatGPT, use either Secure MCP Tunnel or a reachable
HTTPS endpoint with appropriate authentication.

## ChatGPT -> machine: receive_chat_file

The tool declares:

```json
{
  "_meta": {
    "openai/fileParams": ["file"]
  }
}
```

The top-level `file` input follows ChatGPT's current file-param contract:

```json
{
  "download_url": "https://...",
  "file_id": "file_...",
  "mime_type": "application/octet-stream",
  "file_name": "example.bin"
}
```

`download_url` and `file_id` are required. `mime_type` and `file_name`
are declared but optional. ChatGPT injects this object for a file selected or
attached in the conversation, so the model should not invent a temporary URL or
ask the user to paste one.

Example intent:

```text
Copy the file I attached to artifacts/input.zip on the connected machine.
```

The model can call:

```json
{
  "file": "<host supplied file object>",
  "destination_path": "artifacts/input.zip"
}
```

In `ACCESS_MODE=safe`, omitted `dry_run` keeps the normal preview default.
In `ACCESS_MODE=full`, omitted `dry_run` writes immediately. An explicit
`dry_run=true` always previews.

### Import safeguards

The implementation:

- accepts arbitrary binary bytes rather than forcing UTF-8;
- streams the temporary download to a file instead of putting the payload into
  the model context;
- accepts HTTPS download URLs only;
- rejects URLs that resolve to private, loopback, link-local, multicast,
  documentation, benchmarking, or other non-public address ranges;
- revalidates redirects;
- applies the configured byte ceiling while streaming;
- resolves destination paths through the normal workspace perimeter;
- preserves secret-path policy;
- respects `WRITABLE_GLOBS` and its catch-all interlock;
- rejects direct symlink endpoints and symlink escapes;
- never overwrites an existing destination;
- returns byte size and SHA-256 after a successful transfer.

Binary entries in `BINARY_GLOBS` remain blocked for the text-oriented tools,
but are intentionally permitted by the dedicated file-transfer path. This does
not bypass `SECRET_GLOBS`.

## Machine -> ChatGPT: export_file_to_chat

`export_file_to_chat(path=...)` validates the local file, computes its size and
SHA-256, detects a MIME type when possible, and returns an MCP
`resource_link` in the tool result.

The resource URI uses the logical scheme:

```text
chatrepo-file://local/<opaque-path-token>
```

The URI is not a public web URL. ChatGPT/MCP clients fetch it back from the same
MCP server with `resources/read`. Python and Go both register the matching
resource template and re-run path, secret, file-type, and size checks at read
time.

This avoids base64-encoding the complete file directly inside the ordinary tool
result. The MCP `resources/read` response still carries the binary resource
according to the MCP protocol, so client/platform response limits can be lower
than the server-side maximum.

Example intent:

```text
Attach reports/run-42.zip from the connected VPS to this chat.
```

The model calls:

```json
{
  "path": "reports/run-42.zip"
}
```

## Limits

Two independent server-side limits are shared by Python and Go:

| Variable | Default | Direction |
|---|---:|---|
| `FILE_TRANSFER_IMPORT_MAX_BYTES` | 536870912 (512 MiB) | ChatGPT -> machine |
| `FILE_TRANSFER_EXPORT_MAX_BYTES` | 104857600 (100 MiB) | machine -> ChatGPT |

They are intentionally separate from `MAX_FILE_BYTES` and
`MAX_WRITE_FILE_BYTES`, which protect text-oriented read/edit operations.

Change them in `.env` and restart the MCP service when you intentionally want
different ceilings. A ChatGPT or MCP host may impose a lower limit than these
server values.

## Connectivity modes

### OpenAI Secure MCP Tunnel

Recommended for a private PC. The MCP listener can stay on loopback and the
outbound tunnel makes it reachable to ChatGPT without creating public ingress.

### Public HTTPS endpoint

The same two transfer tools work without OpenAI Secure MCP Tunnel. Publish the
normal Streamable HTTP endpoint, for example:

```text
https://mcp.example.com/mcp
```

and connect it in ChatGPT using **Server URL**. Use a real authentication layer
for a full-access server; do not expose a trusted-machine coding agent
anonymously.

No code path in either transfer tool checks for, imports, or depends on
`tunnel-client`.

## Relevant specifications

- OpenAI Plugins reference — file params, file APIs, tool metadata:
  <https://developers.openai.com/plugins/reference>
- OpenAI MCP server guide:
  <https://developers.openai.com/plugins/build/mcp-server>
- OpenAI Secure MCP Tunnel:
  <https://developers.openai.com/api/docs/guides/secure-mcp-tunnels>
- MCP resource links are part of the MCP tool-result content model; the Python
  and Go SDKs expose `ResourceLink` and binary resources directly.
