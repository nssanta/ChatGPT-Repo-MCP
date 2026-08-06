# Bounded output and artifact operations

ChatRepo treats process output as an untrusted stream. A command, search, Git
operation, diagnostic, or terminal must not be able to grow server memory in
proportion to its output.

## Runtime contract

- Redaction happens before durable persistence and before retained in-memory
  previews. Secret tokens and private keys are handled across chunk boundaries.
- Inline stdout, stderr, search records, diagnostics, and log pages are bounded.
- Command, Git, and GitHub responses default to a UTF-8-safe 64 KiB head/tail
  preview (`DEFAULT_INLINE_OUTPUT_BYTES`). This response-layer default does not
  lower the existing hard capture ceilings; `run_command.max_output_chars` may
  explicitly request a larger preview up to its configured hard ceiling.
- Complete redacted output is written below
  `COMMAND_JOBS_DIR/artifacts` when the tool supports durable continuation.
- A partial inline result includes a `receipt` and a ready continuation shaped
  as `{"tool":"read_artifact","arguments":{"artifact_id":"..."}}`.
- `read_artifact` accepts only `artifact_id`, an optional opaque `cursor`, and
  an optional page size. Clients pass `next_cursor` back unchanged until
  `eof=true`; they do not calculate offsets or choose an internal stream.
- Existing stdout/stderr/result fields remain available for compatibility.

Quick text search stops the `rg` process at the global result limit.
`mode=exhaustive` uses the existing background-job lifecycle and can be polled
with `get_job_status` or cancelled with `cancel_command_job`.

## Storage policy

Defaults:

| Setting | Default |
|---|---:|
| `ARTIFACT_TOTAL_BYTES` | 10 GiB |
| `ARTIFACT_MAX_BYTES` | 5 GiB |
| `ARTIFACT_TTL_SECONDS` | 7 days |
| `ARTIFACT_DISK_RESERVE_BYTES` | 2 GiB |

The effective free-space reserve is the larger of
`ARTIFACT_DISK_RESERVE_BYTES` and 10% of the artifact filesystem capacity.
Active artifacts are never evicted. Cleanup removes expired completed
artifacts first, then least-recently-used completed artifacts when space is
needed. Legacy command logs remain readable and are not silently treated as a
complete new-format artifact.

`PERSIST_FULL_OUTPUT=true` is mandatory in this release. The server rejects a
false value at startup rather than pretending that an unavailable continuation
is durable.

## Resource profiles

`RESOURCE_PROFILE=auto` chooses internal limits from effective memory without
changing public tool defaults. On Linux, effective memory is host physical RAM
capped by the current process cgroup v2 `memory.max`/`memory.high` or v1 memory
limit. When cgroup data is unavailable, the runtime falls back to host memory.

| Effective memory | Applied profile | Diagnostic buffer estimate | Enforced heavy operations |
|---|---|---:|---:|
| up to 4 GiB or unknown | `small` | 16 MiB | 2 |
| over 4 GiB through 16 GiB | `medium` | 32 MiB | 4 |
| over 16 GiB | `large` | 64 MiB | 8 |

`MAX_HEAVY_OPERATIONS` is the enforced profile limit. Use
`RESOURCE_PROFILE=custom` with `MAX_HEAVY_OPERATIONS` for an explicit operator
override. `RESOURCE_BUFFER_BYTES` is retained for compatibility as a
telemetry-only diagnostic estimate: it does not reserve memory or enforce a
process-wide buffer limit. Runtime DTOs report this explicitly with
`resource_buffer_enforced=false` and
`resource_buffer_semantics=diagnostic_estimate_only`. When the heavy limit is
occupied, the public result reports `resource_busy`; that enforced limit is
never silently bypassed.

## Audit and incident diagnosis

Heavy operations write a redacted start record before execution and a finish
record with request id, safe argument fingerprint, duration, byte counts, and
status. The command audit rotates at 10 MiB and retains five generations.

For an incident:

1. Match the request id in the audit start/finish records.
2. Check the artifact receipt, status, byte totals, and persistence warnings.
3. Inspect service/cgroup memory events and OOM counters.
4. Reproduce only in a disposable workspace with deterministic bounded
   generators; never run an unbounded producer such as `yes` against live.
5. Page the complete redacted artifact and compare its SHA-256 with metadata.

Quota, reserve, short-write, `ENOSPC`, or I/O failures fail closed with a typed
persistence error. They must not fall back to retaining the missing output in
RAM.
