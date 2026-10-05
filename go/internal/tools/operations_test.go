package tools

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestOperationsIndependentOfHeavySlotsAndSessions(t *testing.T) {
	e, _ := newTestEngine(t)
	ctx := WithOperationIdentity(context.Background(), OperationIdentity{SessionID: "a"})
	ctx, parent := e.operations.start(ctx, "batch_call", nil, "tool", true)
	childContext, child := e.operations.start(ctx, "find_files", nil, "tool", true)
	if e.listHeavyOperations()["used"] != 0 {
		t.Fatal("accounting acquired a heavy slot")
	}
	if e.listOperations(ctx, map[string]any{"scope": "session"})["count"] != 2 {
		t.Fatal("missing own operations")
	}
	other := WithOperationIdentity(context.Background(), OperationIdentity{SessionID: "b"})
	if e.listOperations(other, map[string]any{"scope": "session"})["count"] != 0 {
		t.Fatal("session filter leaked another session")
	}
	if e.cancelOperation(parent.id)["status"] != "cancelling" {
		t.Fatal("cancel not requested")
	}
	if childContext.Err() == nil {
		t.Fatal("child not cancelled")
	}
	if e.listOperations(ctx, nil)["count"] != 2 {
		t.Fatal("cancelling disappeared before worker exit")
	}
	e.operations.finish(child, "cancelled")
	e.operations.finish(parent, "cancelled")
	if e.cancelOperation(parent.id)["status"] != "cancelled" {
		t.Fatal("completed cancel is not idempotent")
	}
	if e.listOperations(ctx, nil)["count"] != 0 {
		t.Fatal("completed operation leaked")
	}
}
func TestOperationWalksStopOnCancellation(t *testing.T) {
	e, root := newTestEngine(t)
	for i := 0; i < 20; i++ {
		if err := os.WriteFile(filepath.Join(root, time.Unix(int64(i), 0).Format("150405")+".txt"), []byte("hello"), 0600); err != nil {
			t.Fatal(err)
		}
	}
	ctx, op := e.operations.start(context.Background(), "recent_changes", nil, "tool", true)
	result := e.recentChanges(ctx, ".", nil, 5)
	if result["count"] != 5 {
		t.Fatalf("wrong result: %#v", result)
	}
	e.cancelOperation(op.id)
	if err := e.operationCheckpoint(ctx, "walking", 1, 0); err == nil {
		t.Fatal("checkpoint ignored cancellation")
	}
	if e.findFilesContext(ctx, "*", ".", true, 100)["error_kind"] != "operation_cancelled" {
		t.Fatal("cancelled find scanned files")
	}
	e.operations.finish(op, "cancelled")
}
func TestOperationHistoryAndSafeMutation(t *testing.T) {
	e, _ := newTestEngine(t)
	_, active := e.operations.start(context.Background(), "write_text_file", map[string]any{"command": "secret"}, "tool", false)
	if e.cancelOperation(active.id)["error_kind"] != "operation_not_cancellable" {
		t.Fatal("write transaction cancelled")
	}
	for i := 0; i < 1002; i++ {
		_, op := e.operations.start(context.Background(), "tree", nil, "tool", true)
		e.operations.finish(op, "completed")
	}
	if e.listOperations(context.Background(), map[string]any{"include_finished": true})["total"] != 1001 {
		t.Fatal("history not bounded")
	}
	if e.getOperation(active.id)["ok"] != true {
		t.Fatal("active write evicted")
	}
	e.operations.finish(active, "completed")
}
func TestTrackedCommandCancellationWaitsForProcess(t *testing.T) {
	if os.PathSeparator == '\\' {
		t.Skip("POSIX command")
	}
	e, _ := newTestEngine(t)
	e.settings.CommandPolicyMode = "unrestricted"
	result := make(chan map[string]any, 1)
	go func() {
		result <- e.Execute(context.Background(), "run_command", map[string]any{"command": "sleep 20"})
	}()
	deadline := time.Now().Add(5 * time.Second)
	var id string
	for time.Now().Before(deadline) {
		list := e.listOperations(context.Background(), nil)
		for _, op := range list["operations"].([]map[string]any) {
			if op["tool"] == "run_command" {
				id = op["operation_id"].(string)
			}
		}
		if id != "" {
			break
		}
		time.Sleep(time.Millisecond)
	}
	if id == "" {
		t.Fatal("command not observable")
	}
	e.cancelOperation(id)
	select {
	case value := <-result:
		if value["error_kind"] != "operation_cancelled" {
			t.Fatalf("bad cancellation result: %#v", value)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("command not stopped")
	}
	if e.getOperation(id)["operation"].(map[string]any)["status"] != "cancelled" {
		t.Fatal("wrong terminal status")
	}
}

func TestReadPathsRespectCancellation(t *testing.T) {
	e, root := newTestEngine(t)
	if err := os.WriteFile(filepath.Join(root, "README.md"), []byte("def example():\n pass\n"), 0600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	for name, result := range map[string]map[string]any{
		"list":      e.listDirectoryContext(ctx, ".", true, 20),
		"read":      e.readTextContext(ctx, "README.md", 1, 0, false),
		"multiple":  e.readMultipleContext(ctx, []string{"README.md"}),
		"metadata":  e.fileMetadataContext(ctx, "README.md", true),
		"symbols":   e.documentSymbolsContext(ctx, "README.md"),
		"bootstrap": e.contextBootstrap(ctx),
	} {
		if result["error_kind"] != "operation_cancelled" {
			t.Fatalf("%s masked cancellation: %#v", name, result)
		}
	}
}
func TestProgressAndProcessLifecycle(t *testing.T) {
	e, _ := newTestEngine(t)
	ctx, op := e.operations.start(context.Background(), "tree", nil, "tool", true)
	if err := e.operationCheckpoint(ctx, "walking", 1, 1); err != nil {
		t.Fatal(err)
	}
	before := e.getOperation(op.id)["operation"].(map[string]any)["last_progress_at"]
	e.cancelOperation(op.id)
	after := e.getOperation(op.id)["operation"].(map[string]any)
	if after["last_progress_at"] != before {
		t.Fatal("cancel request looked like progress")
	}
	e.operationProcess(ctx, 12345)
	if e.getOperation(op.id)["operation"].(map[string]any)["pid"] != 12345 {
		t.Fatal("missing owned PID")
	}
	e.operationProcess(ctx, 0)
	if _, exists := e.getOperation(op.id)["operation"].(map[string]any)["pid"]; exists {
		t.Fatal("stale PID")
	}
	e.operations.finish(op, "completed")
	if e.getOperation(op.id)["operation"].(map[string]any)["status"] != "completed" {
		t.Fatal("late request overwrote completion")
	}
}
func TestCancelledLaunchDoesNotDetachOrSpawn(t *testing.T) {
	e, root := newTestEngine(t)
	e.settings.CommandPolicyMode = "unrestricted"
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	result := e.startJobRequest(ctx, map[string]any{"command": "touch should-not-exist"}, false)
	if result["error_kind"] != "operation_cancelled" {
		t.Fatalf("cancelled launch: %#v", result)
	}
	if len(e.jobs) != 0 {
		t.Fatal("cancelled launch detached a job")
	}
	if _, err := os.Stat(filepath.Join(root, "should-not-exist")); !os.IsNotExist(err) {
		t.Fatal("cancelled launch executed")
	}
}
