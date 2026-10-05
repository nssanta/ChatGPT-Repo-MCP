# Operation lifecycle and cancellation

Python and Go account for server work separately from resource admission. A
filesystem walk can consume CPU without holding a heavy-operation slot, creating
a background command job, or opening a terminal. Empty heavy/job/terminal lists
therefore do not establish that the server is idle.

## Inspect work

```json
{"tool":"list_operations","arguments":{"scope":"server"}}
{"tool":"list_operations","arguments":{"scope":"session"}}
{"tool":"get_operation","arguments":{"operation_id":"<operation UUID>"}}
{"tool":"cancel_operation","arguments":{"operation_id":"<operation UUID>"}}
```

The default scope is `server`; `session` filters by the calling MCP session.
`include_finished=false` lists active work, and `limit=100` limits the response,
not execution. The response includes the total matching count and whether it
was truncated; the maximum page size is 1000. All authenticated clients of this
trusted server can inspect and cancel another session's operation by ID. This
is not a tenant-isolation API.

Records expose an opaque operation UUID, server-instance ID, parent-operation
ID, server-generated request correlation ID, public session ID, client
name/version when available, tool name, kind, project root and safe target paths, start/update
and finish timestamps, age, status, progress, and cancellation capability.
Raw commands, search terms, file contents, environment values and authentication
tokens are not included. Progress reports a phase and visited file/directory
counts, not a percentage based on an unknown total. Updates are coalesced to
avoid formatting a timestamp for every visited file.

Batch children inherit session and request correlation and identify their
parent. Detached jobs and terminals have their own records and native
`resource_id`; launcher completion does not terminate those resources.
Existing heavy-operation entries expose `tracking_operation_id`, linking the
pool holder to its lifecycle record without acquiring another heavy slot.
Maintenance passes are visible as `kind="internal"` while they run. The
registry accounts for tool execution and owned resources, not every SDK or
runtime task and not arbitrary processes elsewhere on the machine. The three registry
inspection/cancellation tools are excluded from the work list itself; existing
management tools are accounted for too, including their artifact/metadata I/O.

The public session ID is separate from the MCP transport session token. A
session is not necessarily a chat or an individual agent, and several clients
can report the same client name. The server does not infer an agent's identity
from these fields. Direct internal calls without an MCP session have a null
session ID; asking for session scope without a session returns
`session_unavailable`.

## Cancellation semantics

`cancel_operation` acknowledges a request. Active work becomes `cancelling` and
remains listed until its worker/process has stopped and performed cleanup.
Completion is confirmed through `get_operation`, not through the acknowledgement.
Repeated cancellation is idempotent; cancelling a completed operation returns
its actual terminal state.

Filesystem enumeration, path checks and search loops check cancellation.
Subprocess cancellation uses the owned process lifecycle; detached jobs and
terminals retain their existing native management APIs. Batch cancellation
stops starting further work and requests cancellation of active tool children;
already detached resources survive launcher/request cancellation.

Python moves blocking tools off the MCP event loop using AnyIO workers, with no
new worker admission ceiling. Batch executor threads inherit the operation
context. Inspection and cancellation do not wait in the work queue. MCP request
cancellation signals a running synchronous worker and waits for that worker to
leave; abandoning an await does not turn a live thread into a finished record.
Go propagates its operation context through dispatch and nested calls.

An OS call can still take time to return. The record stays active during that
interval. File edits, commit/workflow transactions, transfers, desktop actions
and maintenance passes without a safe interruption point advertise
`cancellable=false` and a reason; cancellation returns
`operation_not_cancellable`. Cancellation never promises rollback of completed
writes, subprocess side effects, or external operations. Existing access rules,
heavy-slot capacity, output limits, traversal depth and search semantics remain
in force.

Terminal states are `completed`, `failed`, `cancelled`, or `timed_out`.
Python also exposes `queued` while a worker is being scheduled. The active set
is `queued`, `running`, and `cancelling`.

## History and audit

Finished records are retained in memory for up to 24 hours, capped at 1000.
Active records are never evicted by that history cap. A new process has a new
server-instance ID and does not present old records as running; old IDs return
`operation_not_found`. In-memory history is not restored on restart.

`COMMAND_AUDIT_LOG_PATH` receives `operation_started`,
`operation_cancel_requested`, and `operation_finished` events with operation,
parent, request, session and server-instance IDs, tool, kind and state. Existing
command/heavy audit events are retained and carry the operation/session IDs as
well, so their native request/log IDs can be correlated after restart. Dictionary
tool responses include `tracking_operation_id` and, unless already used by the
legacy interface, `operation_id`; for a launcher this identifies the
launch call, while its detached child is found by the native `resource_id`. The audit uses the existing redaction
and rotation policy (10 MiB, five rotated generations); it contains no raw tool
arguments. `COMMAND_JOBS_DIR` contains native job metadata and output artifacts.

Service stdout/stderr follows the configured service manager; for a systemd
user service, inspect `journalctl --user -u chatrepo-mcp.service`. Git's
`.git/logs` is unrelated to MCP execution logs.

For a CPU incident, inspect `list_operations` first, correlate IDs with audit
events, and use `get_operation` to confirm cancellation or completion. Compare
with OS CPU/process evidence: a pool snapshot alone is not an idle signal.

## Validation and rollout

`make check` validates tests, coverage, schemas, builds and the dual-server HTTP
acceptance suite. The lifecycle acceptance uses synthetic temporary files and
two MCP sessions to exercise unmetered walks, session filtering, batch children,
detached jobs and terminals. `make go-race` checks concurrent Go access.

Use `make check BIN_DIR=/tmp/chatrepo-validation-bin` to keep validation builds
away from an installed binary; the acceptance runner receives
`CHATREPO_GO_BINARY`, and the computer companion uses `COMPUTER_HOST_PATH`.
Committing/pushing source does not replace or restart a running MCP process.
Install and restart through the normal operator deployment procedure after
verification. Updating the connector's catalog may be necessary for new tools
to become available to clients.

The local aggregate Go coverage gate has a known pre-existing failure: revision
`c3a345f` measured 69.0% against its 80% threshold, mainly because the computer
runtime and command entrypoints have little unit coverage. The operation change
raises aggregate coverage; the threshold is not lowered. Report that gate
separately from passing unit, race and HTTP lifecycle checks.
