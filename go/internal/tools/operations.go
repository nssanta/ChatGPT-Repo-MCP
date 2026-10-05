package tools

import (
	"context"
	"crypto/sha256"
	"fmt"
	"sort"
	"sync"
	"time"
)

type operationContextKey struct{}
type identityContextKey struct{}
type OperationIdentity struct {
	SessionID string
	Client    map[string]any
}

func WithOperationIdentity(ctx context.Context, identity OperationIdentity) context.Context {
	safeClient := map[string]any{}
	for _, key := range []string{"name", "version"} {
		if value, ok := identity.Client[key]; ok {
			text, _ := capText(redact(fmt.Sprint(value)), 128)
			safeClient[key] = text
		}
	}
	if len(safeClient) > 0 {
		identity.Client = safeClient
	}
	return context.WithValue(ctx, identityContextKey{}, identity)
}
func PublicSessionID(instance, raw string) string {
	sum := sha256.Sum256([]byte(instance + ":" + raw))
	return fmt.Sprintf("%x", sum[:16])
}
func (e *Engine) ServerInstanceID() string { return e.operations.instanceID }

type trackedOperation struct {
	id              string
	parent          *trackedOperation
	identity        OperationIdentity
	started         time.Time
	finished        time.Time
	finishing       bool
	progressUpdated time.Time
	lastProgress    time.Time
	cancel          context.CancelFunc
	data            map[string]any
}
type operationRegistry struct {
	mu         sync.Mutex
	instanceID string
	entries    map[string]*trackedOperation
	engine     *Engine
}

func newOperationRegistry(e *Engine) *operationRegistry {
	return &operationRegistry{instanceID: randomID(), entries: make(map[string]*trackedOperation), engine: e}
}
func currentOperation(ctx context.Context) *trackedOperation {
	op, _ := ctx.Value(operationContextKey{}).(*trackedOperation)
	return op
}
func isControlTool(name string) bool {
	return name == "list_operations" || name == "get_operation" || name == "cancel_operation"
}
func cancellableTool(name string) bool {
	switch name {
	case "repo_info", "list_dir", "tree", "read_text_file", "read_multiple_files", "file_metadata", "find_files", "search_text", "symbol_search", "recent_changes", "todo_scan", "dependency_map", "list_repos", "context_bootstrap", "batch_call", "doctor", "smoke_all", "workspace_symbols", "symbol_definition", "document_symbols", "code_diagnostics", "git_status", "git_diff", "git_log", "git_show", "git_branches", "git_blame", "git_grep", "run_command", "run_commands", "run_test_preset", "run_quality_gate", "scan_new_policy_violations", "gh_status", "gh_checks", "gh_pr_list", "gh_pr_view", "gh_issue_list", "gh_issue_view", "gh_run_view", "read_artifact", "get_command_log", "summarize_command_log", "git_worktree_guard", "list_test_presets":
		return true
	}
	return false
}
func (r *operationRegistry) pruneLocked(now time.Time) {
	completed := make([]*trackedOperation, 0)
	for _, op := range r.entries {
		if !op.finished.IsZero() {
			completed = append(completed, op)
		}
	}
	sort.Slice(completed, func(i, j int) bool { return completed[i].finished.Before(completed[j].finished) })
	for index, op := range completed {
		if index < len(completed)-1000 || now.Sub(op.finished) > 24*time.Hour {
			delete(r.entries, op.id)
		}
	}
}
func (r *operationRegistry) start(parentContext context.Context, name string, args map[string]any, kind string, cancellable bool) (context.Context, *trackedOperation) {
	parent := currentOperation(parentContext)
	identity, _ := parentContext.Value(identityContextKey{}).(OperationIdentity)
	if parent != nil {
		identity = parent.identity
	}
	ctx, cancel := context.WithCancel(parentContext)
	op := &trackedOperation{id: randomID(), parent: parent, identity: identity, started: time.Now(), cancel: cancel}
	root, _ := capText(redact(r.engine.settings.ProjectRoot), 512)
	target := map[string]any{"project_root": root}
	switch name {
	case "tree", "find_files", "recent_changes", "todo_scan", "search_text", "symbol_search", "dependency_map", "list_dir":
		target["path"] = "."
	}
	for _, key := range []string{"path", "paths", "repo", "cwd"} {
		if value, ok := args[key]; ok {
			text, _ := capText(redact(fmt.Sprint(value)), 512)
			target[key] = text
		}
	}
	requestID := op.id
	var parentID any
	if parent != nil {
		parentID = parent.id
		r.mu.Lock()
		requestID = parent.data["request_id"].(string)
		r.mu.Unlock()
	}
	var reason any
	if !cancellable {
		reason = "No safe interruption point; already applied changes are not rolled back."
	}
	var sessionID any
	if identity.SessionID != "" {
		sessionID = identity.SessionID
	}
	op.data = map[string]any{"operation_id": op.id, "server_instance_id": r.instanceID, "parent_operation_id": parentID, "request_id": requestID, "session_id": sessionID, "client": identity.Client, "tool": name, "kind": kind, "target": target, "started_at": op.started.Format(time.RFC3339Nano), "updated_at": op.started.UTC().Format(time.RFC3339Nano), "last_progress_at": op.started.UTC().Format(time.RFC3339Nano), "status": "running", "cancellable": cancellable, "cancel_reason": reason, "cancel_requested": false, "progress": map[string]any{"phase": "starting", "files": int64(0), "directories": int64(0)}}
	op.lastProgress = op.started
	r.mu.Lock()
	r.pruneLocked(time.Now())
	r.entries[op.id] = op
	r.mu.Unlock()
	r.audit("operation_started", op)
	return context.WithValue(ctx, operationContextKey{}, op), op
}
func (r *operationRegistry) audit(event string, op *trackedOperation) { r.auditStatus(event, op, "") }
func (r *operationRegistry) auditStatus(event string, op *trackedOperation, status string) {
	r.mu.Lock()
	payload := map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "event": event}
	for _, key := range []string{"operation_id", "server_instance_id", "parent_operation_id", "request_id", "session_id", "tool", "kind", "status"} {
		payload[key] = op.data[key]
	}
	if status != "" {
		payload["status"] = status
	}
	r.mu.Unlock()
	r.engine.appendAudit(payload)
}
func (r *operationRegistry) finish(op *trackedOperation, status string) {
	r.mu.Lock()
	if !op.finished.IsZero() || op.finishing {
		r.mu.Unlock()
		return
	}
	op.finishing = true
	cancel := op.cancel
	r.mu.Unlock()
	cancel()
	r.auditStatus("operation_finished", op, status)
	r.mu.Lock()
	op.finished = time.Now().UTC()
	op.data["status"] = status
	op.data["finished_at"] = op.finished.Format(time.RFC3339Nano)
	op.data["updated_at"] = op.data["finished_at"]
	r.mu.Unlock()
}
func (r *operationRegistry) snapshotLocked(op *trackedOperation) map[string]any {
	result := make(map[string]any, len(op.data)+1)
	for key, value := range op.data {
		result[key] = value
	}
	progress := map[string]any{}
	for key, value := range op.data["progress"].(map[string]any) {
		progress[key] = value
	}
	result["progress"] = progress
	if op.finishing && op.finished.IsZero() {
		result["cancellable"] = false
		result["cancel_reason"] = "Worker has stopped and audit output is being finalized"
		progress["phase"] = "finalizing"
	}
	end := op.finished
	if end.IsZero() {
		end = time.Now()
	}
	result["age_ms"] = end.Sub(op.started).Milliseconds()
	result["last_progress_age_ms"] = max(int64(0), end.Sub(op.lastProgress).Milliseconds())
	return result
}
func (e *Engine) listOperations(ctx context.Context, args map[string]any) map[string]any {
	scope := stringArg(args, "scope", "server")
	identity, _ := ctx.Value(identityContextKey{}).(OperationIdentity)
	if scope != "server" && scope != "session" {
		return failure("invalid_scope", "scope must be server or session")
	}
	if scope == "session" && identity.SessionID == "" {
		return failure("session_unavailable", "No MCP session is associated with this call")
	}
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	r.pruneLocked(time.Now())
	selected := make([]*trackedOperation, 0)
	for _, op := range r.entries {
		if (boolArg(args, "include_finished", false) || op.finished.IsZero()) && (scope == "server" || op.identity.SessionID == identity.SessionID) {
			selected = append(selected, op)
		}
	}
	sort.Slice(selected, func(i, j int) bool { return selected[i].started.After(selected[j].started) })
	limit := min(max(intArg(args, "limit", 100), 1), 1000)
	results := make([]map[string]any, 0)
	for _, op := range selected[:min(limit, len(selected))] {
		results = append(results, r.snapshotLocked(op))
	}
	return map[string]any{"ok": true, "server_instance_id": r.instanceID, "scope": scope, "operations": results, "count": len(results), "total": len(selected), "truncated": len(selected) > limit}
}
func (e *Engine) getOperation(id string) map[string]any {
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	r.pruneLocked(time.Now())
	op := r.entries[id]
	if op == nil {
		return failure("operation_not_found", "Operation is unknown or its history expired")
	}
	return map[string]any{"ok": true, "operation": r.snapshotLocked(op)}
}
func (e *Engine) cancelOperation(id string) map[string]any {
	r := e.operations
	r.mu.Lock()
	op := r.entries[id]
	if op == nil {
		r.mu.Unlock()
		return failure("operation_not_found", "Operation is unknown or its history expired")
	}
	if !op.finished.IsZero() {
		result := map[string]any{"ok": true, "operation_id": id, "cancel_requested": op.data["cancel_requested"], "status": op.data["status"]}
		r.mu.Unlock()
		return result
	}
	if op.finishing {
		r.mu.Unlock()
		return failure("operation_not_cancellable", "Worker has stopped and audit output is being finalized")
	}
	if op.data["cancellable"] != true {
		reason := fmt.Sprint(op.data["cancel_reason"])
		r.mu.Unlock()
		return failure("operation_not_cancellable", reason)
	}
	first := op.data["cancel_requested"] != true
	op.data["cancel_requested"] = true
	op.data["status"] = "cancelling"
	op.data["updated_at"] = time.Now().UTC().Format(time.RFC3339Nano)
	cancel := op.cancel
	children := []string{}
	for _, child := range r.entries {
		if child.parent == op && child.finished.IsZero() && child.data["kind"] == "tool" && child.data["cancellable"] == true {
			children = append(children, child.id)
		}
	}
	r.mu.Unlock()
	if first {
		r.audit("operation_cancel_requested", op)
		cancel()
	}
	for _, child := range children {
		e.cancelOperation(child)
	}
	return map[string]any{"ok": true, "operation_id": id, "cancel_requested": true, "status": "cancelling"}
}
func (e *Engine) noteResourceCancel(resourceID string) {
	r := e.operations
	r.mu.Lock()
	matched := []*trackedOperation{}
	for _, op := range r.entries {
		if op.data["resource_id"] == resourceID && op.finished.IsZero() && op.data["cancel_requested"] != true {
			op.data["cancel_requested"] = true
			op.data["status"] = "cancelling"
			op.data["updated_at"] = time.Now().UTC().Format(time.RFC3339Nano)
			matched = append(matched, op)
		}
	}
	r.mu.Unlock()
	for _, op := range matched {
		r.audit("operation_cancel_requested", op)
	}
}

func (e *Engine) operationCheckpoint(ctx context.Context, phase string, files, directories int64) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	op := currentOperation(ctx)
	if op == nil {
		return nil
	}
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	progress := op.data["progress"].(map[string]any)
	changed := phase != "" && progress["phase"] != phase || files != 0 || directories != 0
	if phase != "" {
		progress["phase"] = phase
	}
	progress["files"] = progress["files"].(int64) + files
	progress["directories"] = progress["directories"].(int64) + directories
	now := time.Now()
	if changed {
		op.lastProgress = now
	}
	if changed && now.Sub(op.progressUpdated) >= 250*time.Millisecond {
		op.data["updated_at"] = now.UTC().Format(time.RFC3339Nano)
		op.data["last_progress_at"] = op.lastProgress.UTC().Format(time.RFC3339Nano)
		op.progressUpdated = now
	}
	return nil
}
func (e *Engine) executeTracked(ctx context.Context, name string, args map[string]any) (result map[string]any) {
	if isControlTool(name) {
		return e.executeUntracked(ctx, name, args)
	}
	ctx, op := e.operations.start(ctx, name, args, "tool", cancellableTool(name))
	defer func() {
		status := "completed"
		if result == nil || result["ok"] == false {
			status = "failed"
		}
		if result != nil && result["error_kind"] == "operation_cancelled" {
			status = "cancelled"
		}
		if result != nil && result["timed_out"] == true {
			status = "timed_out"
		}
		e.operations.mu.Lock()
		acknowledged := op.data["cancel_acknowledged"] == true
		e.operations.mu.Unlock()
		if ctx.Err() != nil && cancellableTool(name) && (result == nil || result["ok"] != true || acknowledged) {
			e.cancelOperation(op.id)
			status = "cancelled"
			if result == nil {
				result = map[string]any{}
			}
			result["ok"] = false
			result["error_kind"] = "operation_cancelled"
			result["error"] = "Operation stopped; already applied changes are not rolled back"
		}
		e.operations.finish(op, status)
		if result != nil {
			if _, exists := result["operation_id"]; !exists {
				result["operation_id"] = op.id
			}
			result["tracking_operation_id"] = op.id
		}
	}()
	if ctx.Err() != nil {
		return failure("operation_cancelled", "Operation cancelled before execution")
	}
	return e.executeUntracked(ctx, name, args)
}

// Process handles are informational; cancellation uses the executor's own handle.
func (e *Engine) operationProcess(ctx context.Context, pid int) {
	op := currentOperation(ctx)
	if op == nil {
		return
	}
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	if pid > 0 {
		op.data["pid"] = pid
		op.data["pgid"] = pid
		op.data["progress"].(map[string]any)["phase"] = "waiting_process"
	} else {
		delete(op.data, "pid")
		delete(op.data, "pgid")
	}
}
func (e *Engine) operationCancelError(op *trackedOperation, result map[string]any) {
	if op == nil || result["ok"] != false {
		return
	}
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	if !op.finished.IsZero() {
		return
	}
	message, _ := capText(redact(fmt.Sprint(result["error"])), 512)
	op.data["cancel_error"] = message
	op.data["updated_at"] = time.Now().UTC().Format(time.RFC3339Nano)
}

func (e *Engine) acknowledgeCancellation(ctx context.Context) {
	op := currentOperation(ctx)
	if op == nil {
		return
	}
	e.operations.mu.Lock()
	op.data["cancel_acknowledged"] = true
	e.operations.mu.Unlock()
}
func (e *Engine) operationActivity(ctx context.Context, phase string, size int64) {
	op := currentOperation(ctx)
	if op == nil {
		return
	}
	r := e.operations
	r.mu.Lock()
	defer r.mu.Unlock()
	op.lastProgress = time.Now()
	op.data["last_progress_at"] = op.lastProgress.UTC().Format(time.RFC3339Nano)
	p := op.data["progress"].(map[string]any)
	p["phase"] = phase
	n, _ := p["bytes"].(int64)
	p["bytes"] = n + size
}
