//go:build !windows

package tools

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEffectivePathPrefersExplicitExtra(t *testing.T) {
	root := t.TempDir()
	extra := filepath.Join(root, "extra")
	if err := os.MkdirAll(extra, 0o755); err != nil {
		t.Fatal(err)
	}
	binary := filepath.Join(extra, "demo-tool")
	if err := os.WriteFile(binary, []byte("#!/bin/sh\necho demo-1\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	settings := testSettings(root)
	settings.MCPExtraPath = []string{extra}
	engine := New(settings, nil)
	entries, warnings := engine.effectivePath()
	if len(warnings) != 0 || len(entries) == 0 || entries[0].Path != extra || entries[0].Source != "explicit_extra" {
		t.Fatalf("unexpected path: %#v %#v", entries, warnings)
	}
	status := engine.toolStatus("demo-tool")
	if status["available"] != true || status["source"] != "explicit_extra" {
		t.Fatalf("unexpected status: %#v", status)
	}
}

func TestTerminalCursorWriteResizeAndExit(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.AccessMode = "full"
	settings.EnablePTY = true
	settings.MaxTerminalSessions = 2
	settings.KillGrace = 25 * time.Millisecond
	engine := New(settings, nil)
	started := engine.startTerminal(map[string]any{"command": "read value; echo got:$value", "idle_timeout_ms": 5000})
	if started["ok"] != true {
		t.Fatal(started)
	}
	logID := started["log_id"].(string)
	artifact, ok := started["artifact"].(map[string]any)
	if !ok || artifact["artifact_id"] != logID || artifact["has_more"] != true || artifact["eof"] != false {
		t.Fatalf("terminal start has no truthful durable artifact: %#v", started)
	}
	receipt, ok := artifact["receipt"].(map[string]any)
	if !ok || receipt["status"] != "partial" || receipt["reason"] != "source_active" || receipt["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("terminal start artifact claims completion: %#v", artifact)
	}
	if _, err := os.Stat(filepath.Join(settings.CommandJobsDir, "artifacts", logID+".records")); err != nil {
		t.Fatalf("terminal returned before its canonical artifact existed: %v", err)
	}
	livePage := engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": logID})
	if livePage["ok"] != true || livePage["metadata"].(map[string]any)["kind"] != "pty" || livePage["metadata"].(map[string]any)["ordering"] != "capture_order" {
		t.Fatalf("live terminal artifact metadata is not truthful: %#v", livePage)
	}
	id := started["session_id"].(string)
	if result := engine.writeTerminal(id, "hello\n", "utf8"); result["ok"] != true {
		t.Fatal(result)
	}
	if result := engine.resizeTerminal(id, 100, 30); result["ok"] != true {
		t.Fatal(result)
	}
	cursor := 0
	data := ""
	deadline := time.Now().Add(2 * time.Second)
	for !strings.Contains(data, "got:hello") && time.Now().Before(deadline) {
		read := engine.readTerminal(id, cursor, 65536, 250)
		data += read["data"].(string)
		cursor = read["next_cursor"].(int)
	}
	if !strings.Contains(data, "got:hello") {
		t.Fatalf("unexpected PTY data: %q", data)
	}
	deadline = time.Now().Add(2 * time.Second)
	foundLiveRecords := false
	for time.Now().Before(deadline) {
		livePage = engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": logID})
		records := livePage["payload"].(map[string]any)["records"].([]map[string]any)
		if len(records) > 0 {
			foundLiveRecords = true
			for _, record := range records {
				if record["stream"] != "combined" {
					t.Fatalf("live terminal artifact exposed split stream: %#v", record)
				}
			}
			break
		}
		time.Sleep(10 * time.Millisecond)
	}
	if !foundLiveRecords {
		t.Fatal("live terminal artifact did not expose committed records")
	}
	closed := engine.closeTerminal(id, "SIGTERM", 25*time.Millisecond, false)
	if closed["ok"] != true {
		t.Fatal(closed)
	}
	page := engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": logID})
	if page["ok"] != true || page["metadata"].(map[string]any)["kind"] != "pty" || page["metadata"].(map[string]any)["ordering"] != "capture_order" {
		t.Fatalf("terminal artifact metadata is not truthful: %#v", page)
	}
	records := page["payload"].(map[string]any)["records"].([]map[string]any)
	for _, record := range records {
		if record["stream"] != "combined" {
			t.Fatalf("terminal artifact exposed split stream: %#v", record)
		}
	}
}

func TestTerminalUsesSharedHeavyLimiter(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.AccessMode = "full"
	settings.EnablePTY = true
	settings.MaxTerminalSessions = 2
	settings.MaxHeavyOperations = 1
	engine := New(settings, nil)
	lease, acquired := engine.acquireHeavyOperation()
	if !acquired {
		t.Fatal("failed to reserve heavy slot")
	}
	defer lease.Release()
	result := engine.startTerminal(map[string]any{"command": "sleep 1"})
	if result["error_kind"] != "resource_busy" {
		t.Fatalf("terminal bypassed shared heavy limit: %#v", result)
	}
}

func TestTerminalArtifactQuotaFailureNeverPublishesCompletion(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.AccessMode = "full"
	settings.EnablePTY = true
	settings.MaxTerminalSessions = 1
	settings.ArtifactMaxBytes = 64
	settings.ArtifactTotalBytes = 1024 * 1024
	settings.ArtifactDiskReserveBytes = 0
	engine := New(settings, nil)
	started := engine.startTerminal(map[string]any{
		"command": "i=0; while [ $i -lt 100 ]; do printf 'quota-line-%s\\n' \"$i\"; i=$((i+1)); done",
	})
	if started["ok"] != true {
		t.Fatal(started)
	}
	id := started["session_id"].(string)
	logID := started["log_id"].(string)
	session, err := engine.terminal(id)
	if err != nil {
		t.Fatal(err)
	}
	select {
	case <-session.done:
	case <-time.After(3 * time.Second):
		t.Fatal("terminal did not stop after artifact quota failure")
	}
	if _, err := os.Stat(filepath.Join(settings.CommandJobsDir, "artifacts", logID+".complete")); !os.IsNotExist(err) {
		t.Fatalf("quota-truncated PTY published completion marker: %v", err)
	}
	session.mu.RLock()
	status := session.Status
	session.mu.RUnlock()
	if status != "failed" {
		t.Fatalf("quota-truncated PTY status=%q, want failed", status)
	}
	restarted := New(settings, nil)
	page := restarted.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": logID})
	if page["ok"] != true || page["eof"] != false || page["has_more"] != true {
		t.Fatalf("restart upgraded quota-truncated PTY: %#v", page)
	}
	metadata := page["metadata"].(map[string]any)
	receipt := page["receipt"].(map[string]any)
	if metadata["sha256"] != nil || receipt["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("quota-truncated PTY exposed final truth: %#v", page)
	}
}

func TestPrepareTaskWorktreeUsesExactBase(t *testing.T) {
	root := t.TempDir()
	run := func(args ...string) string {
		command := exec.Command("git", args...)
		command.Dir = root
		output, err := command.CombinedOutput()
		if err != nil {
			t.Fatalf("git %v: %s", args, output)
		}
		return strings.TrimSpace(string(output))
	}
	run("init", "-q", "-b", "main")
	run("config", "user.email", "test@example.com")
	run("config", "user.name", "Test")
	if err := os.WriteFile(filepath.Join(root, "tracked.txt"), []byte("base\n"), 0644); err != nil {
		t.Fatal(err)
	}
	run("add", "tracked.txt")
	run("commit", "-qm", "base")
	base := run("rev-parse", "HEAD")
	if err := os.WriteFile(filepath.Join(root, "tracked.txt"), []byte("dirty\n"), 0644); err != nil {
		t.Fatal(err)
	}
	settings := testSettings(root)
	settings.AccessMode = "full"
	engine := New(settings, nil)
	result := engine.prepareTaskWorktree(context.Background(), root, map[string]any{"branch": "agent/test", "task_name": "task", "base": "main", "dry_run": false, "confirmed": true})
	if result["ok"] != true || result["base_sha"] != base || result["parent_dirty"] != true {
		t.Fatalf("unexpected result: %#v", result)
	}
	data, err := os.ReadFile(filepath.Join(root, ".chatrepo-worktrees", "task", "tracked.txt"))
	if err != nil || string(data) != "base\n" {
		t.Fatalf("unexpected isolated file: %q %v", data, err)
	}
}

func TestBatchCallDefaultsToParallelAndPreservesOrder(t *testing.T) {
	engine, root := newTestEngine(t)
	_ = os.WriteFile(filepath.Join(root, "a.txt"), []byte("a"), 0644)
	_ = os.WriteFile(filepath.Join(root, "b.txt"), []byte("b"), 0644)
	result := engine.batchCall(context.Background(), map[string]any{"calls": []map[string]any{{"tool": "file_metadata", "args": map[string]any{"path": "a.txt"}}, {"tool": "file_metadata", "args": map[string]any{"path": "b.txt"}}}})
	if result["ok"] != true || result["execution"] != "parallel" {
		t.Fatalf("unexpected batch: %#v", result)
	}
	items := result["results"].([]map[string]any)
	if items[0]["index"] != 0 || items[1]["index"] != 1 {
		t.Fatalf("order changed: %#v", items)
	}
}

func TestBatchExecuteHonorsLightWorkerConcurrency(t *testing.T) {
	calls := make([]map[string]any, 6)
	for index := range calls {
		calls[index] = map[string]any{"index": index}
	}
	started := make(chan struct{}, len(calls))
	release := make(chan struct{})
	done := make(chan struct{})
	go func() {
		batchExecute(calls, 6, func(_ int, _ map[string]any) {
			started <- struct{}{}
			<-release
		})
		close(done)
	}()
	for index := 0; index < len(calls); index++ {
		select {
		case <-started:
		case <-time.After(time.Second):
			close(release)
			t.Fatalf("light batch worker pool stopped at %d/%d", index, len(calls))
		}
	}
	close(release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("batch workers did not complete")
	}
}

func TestCodeDiagnosticsUsesExplicitExtraPath(t *testing.T) {
	root := t.TempDir()
	extra := filepath.Join(root, "toolchain")
	if err := os.MkdirAll(extra, 0o755); err != nil {
		t.Fatal(err)
	}
	pyright := filepath.Join(extra, "pyright")
	if err := os.WriteFile(pyright, []byte("#!/bin/sh\nprintf '[]'\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	settings := testSettings(root)
	settings.MCPExtraPath = []string{extra}
	engine := New(settings, nil)
	result := engine.codeDiagnostics(context.Background(), map[string]any{"language": "python"})
	checks := result["checks"].([]map[string]any)
	if len(checks) != 1 || checks[0]["tool"] != "pyright" || checks[0]["path"] != pyright || checks[0]["path_source"] != "explicit_extra" || checks[0]["ok"] != true {
		t.Fatalf("code diagnostics ignored MCP_EXTRA_PATH: %#v", result)
	}
}

func TestGitHubUsesExplicitExtraPath(t *testing.T) {
	root := t.TempDir()
	extra := filepath.Join(root, "toolchain")
	if err := os.MkdirAll(extra, 0o755); err != nil {
		t.Fatal(err)
	}
	gh := filepath.Join(extra, "gh")
	if err := os.WriteFile(gh, []byte("#!/bin/sh\nif [ \"$1 $2\" = \"auth status\" ]; then printf 'authenticated'; else printf '{\\\"resources\\\":{}}'; fi\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	settings := testSettings(root)
	settings.MCPExtraPath = []string{extra}
	settings.GitHubToolsEnabled = true
	engine := New(settings, nil)
	result := engine.Execute(context.Background(), "gh_status", map[string]any{})
	if result["ok"] != true || result["authenticated"] != true {
		t.Fatalf("GitHub tool ignored MCP_EXTRA_PATH: %#v", result)
	}
}
