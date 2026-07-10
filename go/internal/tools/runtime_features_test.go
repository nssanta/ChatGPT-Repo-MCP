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
	closed := engine.closeTerminal(id, "SIGTERM", 25*time.Millisecond, false)
	if closed["ok"] != true {
		t.Fatal(closed)
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
