//go:build !windows

package tools

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"
	"unicode/utf8"
)

func TestSearchWithRipgrepStopsAtGlobalLimit(t *testing.T) {
	root := t.TempDir()
	binary := filepath.Join(root, "rg")
	script := "#!/bin/sh\ni=0\nwhile [ $i -lt 1000000 ]; do printf '{\"type\":\"match\",\"data\":{\"path\":{\"text\":\"" + root + "/x\"},\"lines\":{\"text\":\"hit\"},\"line_number\":1}}\\n'; i=$((i+1)); done\n"
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", root+string(os.PathListSeparator)+os.Getenv("PATH"))
	engine, _ := newTestEngine(t)
	started := time.Now()
	result := engine.searchWithRipgrep(context.Background(), "hit", []string{root}, false, true, 3)
	if result["ok"] != true || result["count"] != 3 || result["truncated"] != true {
		t.Fatalf("unexpected bounded search: %#v", result)
	}
	if time.Since(started) > 5*time.Second {
		t.Fatalf("search did not terminate producer promptly: %s", time.Since(started))
	}
}

func TestRunProcessBoundsHugeStdoutAndRedacts(t *testing.T) {
	result := runProcess(context.Background(), t.TempDir(), 5*time.Second, nil, "sh", "-c", "printf 'token=super-secret\\n'; yes x | head -c 5000000")
	if len(result.Stdout) > 1024*1024 || strings.Contains(result.Stdout, "super-secret") {
		t.Fatalf("unbounded or leaked output: bytes=%d", len(result.Stdout))
	}
}

func TestCommandAuditHasLifecycleFieldsAndRotates(t *testing.T) {
	root := t.TempDir()
	engine, _ := newTestEngine(t)
	engine.settings.CommandAuditLogPath = filepath.Join(root, "audit.log")
	if err := os.WriteFile(engine.settings.CommandAuditLogPath, []byte(strings.Repeat("x", 10*1024*1024)), 0o600); err != nil {
		t.Fatal(err)
	}
	engine.writeCommandAudit("start", "request-1", "run_command", "echo token=secret-value", root, 0, 0, 0, "running")
	data, err := os.ReadFile(engine.settings.CommandAuditLogPath)
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	for _, field := range []string{"request_id", "args_fingerprint", "duration_ms", "stdout_bytes", "stderr_bytes", "status"} {
		if !strings.Contains(text, fmt.Sprintf("\"%s\"", field)) {
			t.Fatalf("missing %s: %s", field, text)
		}
	}
	if strings.Contains(text, "secret-value") {
		t.Fatalf("audit leaked secret: %s", text)
	}
	if _, err := os.Stat(engine.settings.CommandAuditLogPath + ".1"); err != nil {
		t.Fatalf("rotation missing: %v", err)
	}
}

func TestExhaustiveSearchReusesBackgroundJobLifecycle(t *testing.T) {
	bin := t.TempDir()
	rg := filepath.Join(bin, "rg")
	argsPath := filepath.Join(bin, "args")
	cwdPath := filepath.Join(bin, "cwd")
	script := "#!/bin/sh\nprintf '%s\\n' \"$@\" > \"$RG_ARGS\"\npwd > \"$RG_CWD\"\nprintf '{\"type\":\"match\"}\\n'\n"
	if err := os.WriteFile(rg, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	t.Setenv("RG_ARGS", argsPath)
	t.Setenv("RG_CWD", cwdPath)
	engine, root := newTestEngine(t)
	if err := os.MkdirAll(filepath.Join(root, "nested"), 0o700); err != nil {
		t.Fatal(err)
	}
	result := engine.startExhaustiveSearch(context.Background(), map[string]any{"query": "--glob=*.secret", "path": "nested", "mode": "exhaustive"})
	if result["ok"] != true || result["mode"] != "exhaustive" || result["job_id"] == nil || result["artifact"] == nil || result["continuation"] == nil {
		t.Fatalf("missing background lifecycle: %#v", result)
	}
	jobID := result["job_id"].(string)
	entry := engine.jobs[jobID]
	select {
	case <-entry.done:
	case <-time.After(5 * time.Second):
		t.Fatal("exhaustive job did not finish")
	}
	status := engine.getJob(jobID, 10, false)
	if status["status"] != "completed" {
		t.Fatalf("unexpected job status: %#v", status)
	}
	arguments, err := os.ReadFile(argsPath)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(arguments), "--ignore-case\n") || !strings.HasSuffix(string(arguments), "--\n--glob=*.secret\nnested\n") {
		t.Fatalf("exhaustive arguments lost default/canonical target: %q", arguments)
	}
	cwd, err := os.ReadFile(cwdPath)
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(cwd)) != root {
		t.Fatalf("exhaustive cwd escaped canonical root: %q", cwd)
	}
	continuation := result["continuation"].(map[string]any)
	artifactID := result["artifact"].(map[string]any)["artifact_id"]
	if continuation["tool"] != "read_artifact" || continuation["arguments"].(map[string]any)["artifact_id"] != artifactID {
		t.Fatalf("non-canonical exhaustive continuation: %#v", continuation)
	}
	outside := engine.startExhaustiveSearch(context.Background(), map[string]any{"query": "needle", "path": filepath.Join(root, "..")})
	if outside["ok"] != false || outside["error_kind"] != "path_not_allowed" {
		t.Fatalf("outside exhaustive path was accepted: %#v", outside)
	}
}

func TestReadArtifactRejectsNonOpaqueIDsBeforeFilesystemAccess(t *testing.T) {
	engine, root := newTestEngine(t)
	for _, id := range []string{"", ".", "..", "../escape", `..\\escape`, "/absolute", strings.Repeat("a", 129), "bad id"} {
		result := engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": id})
		if result["ok"] != false || result["error_kind"] != "invalid_artifact_id" {
			t.Fatalf("invalid artifact id %q was not rejected: %#v", id, result)
		}
	}
	if _, err := os.Stat(filepath.Join(root, ".jobs", "escape.records")); !os.IsNotExist(err) {
		t.Fatalf("invalid artifact id reached filesystem: %v", err)
	}
	valid := randomID()
	if !artifactIDPattern.MatchString(valid) {
		t.Fatalf("internal artifact id violates public validator: %q", valid)
	}
}

func TestExternalBackgroundJobCannotClaimInternalPolicyExemption(t *testing.T) {
	engine, _ := newTestEngine(t)
	result := engine.Execute(context.Background(), "start_command_job", map[string]any{"command": "sudo true", "background": true})
	if result["ok"] != false || result["error_kind"] == nil {
		t.Fatalf("external background flag bypassed command policy: %#v", result)
	}
}

func TestSharedHeavyLimiterCoversBackgroundRunSearchAndArtifactProcesses(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.MaxHeavyOperations = 1
	engine.heavySlots = make(chan struct{}, 1)
	held := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	if held["ok"] != true {
		t.Fatal(held)
	}
	jobID := held["job_id"].(string)
	defer engine.cancelJob(jobID)

	run := engine.runCommand(context.Background(), commandRequest{Command: "printf ok", Timeout: time.Second, CWD: ".", MaxOutput: 64, TailLines: -1, PolicyExempt: true})
	if run["error_kind"] != "resource_busy" {
		t.Fatalf("run_command bypassed shared heavy limit: %#v", run)
	}
	search := engine.searchText(context.Background(), "needle", ".", nil, false, false, 1)
	if search["error_kind"] != "resource_busy" {
		t.Fatalf("quick search bypassed shared heavy limit: %#v", search)
	}
	_, _, _, _, err := engine.runArtifactProcess(context.Background(), "git_test", root, time.Second, nil, 64, "git", "--version")
	if err == nil || !strings.Contains(err.Error(), "resource_busy") {
		t.Fatalf("artifact subprocess bypassed shared heavy limit: %v", err)
	}
}

func TestRunningJobReceiptIsIncompleteAndContinuable(t *testing.T) {
	engine, _ := newTestEngine(t)
	result := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	if result["ok"] != true {
		t.Fatal(result)
	}
	defer engine.cancelJob(result["job_id"].(string))
	receipt := result["receipt"].(map[string]any)
	if receipt["status"] != "partial" || receipt["completeness"] != "partial" || receipt["reason"] != "source_active" || receipt["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("running receipt claims completion: %#v", receipt)
	}
	continuation, ok := result["continuation"].(map[string]any)
	if !ok || continuation["tool"] != "read_artifact" {
		t.Fatalf("running job has no durable continuation: %#v", result)
	}
}

func TestHeavyLimiterUsesCapacityAndBatchDoesNotDoubleAcquire(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.MaxHeavyOperations = 2
	engine.heavySlots = make(chan struct{}, 2)
	first := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	second := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	if first["ok"] != true || second["ok"] != true {
		t.Fatalf("capacity=2 serialized background jobs: first=%#v second=%#v", first, second)
	}
	third := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	if third["error_kind"] != "resource_busy" {
		t.Fatalf("third operation exceeded capacity=2: %#v", third)
	}
	engine.cancelJob(first["job_id"].(string))
	engine.cancelJob(second["job_id"].(string))

	if err := os.WriteFile(filepath.Join(root, "needle.txt"), []byte("needle\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine.settings.MaxHeavyOperations = 1
	engine.heavySlots = make(chan struct{}, 1)
	batch := engine.batchCall(context.Background(), map[string]any{
		"execution": "parallel", "max_concurrency": 4,
		"calls": []any{map[string]any{"tool": "search_text", "args": map[string]any{"query": "needle", "path": ".", "limit": 1}}},
	})
	if batch["ok"] != true {
		t.Fatalf("batch container double-acquired heavy lease: %#v", batch)
	}
}

func TestSaturatedMixedBatchKeepsTypedBusyResultNested(t *testing.T) {
	engine, root := newTestEngine(t)
	if output, err := exec.Command("git", "-C", root, "init", "-q").CombinedOutput(); err != nil {
		t.Fatalf("init fixture repo: %v: %s", err, output)
	}
	if err := os.WriteFile(filepath.Join(root, "readme.txt"), []byte("fixture\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	engine.settings.MaxHeavyOperations = 1
	engine.heavySlots = make(chan struct{}, 1)
	held := engine.startInternalJob(context.Background(), map[string]any{"command": "sleep 5"})
	if held["ok"] != true {
		t.Fatal(held)
	}
	defer engine.cancelJob(held["job_id"].(string))

	batch := engine.batchCall(context.Background(), map[string]any{
		"execution": "parallel", "max_concurrency": 6,
		"calls": []any{
			map[string]any{"tool": "file_metadata", "args": map[string]any{"path": "readme.txt"}},
			map[string]any{"tool": "git_diff", "args": map[string]any{"repo": "."}},
		},
	})
	items := batch["results"].([]map[string]any)
	if batch["ok"] != false || items[0]["ok"] != true || items[1]["ok"] != false {
		t.Fatalf("unexpected mixed saturated batch: %#v", batch)
	}
	busy, ok := items[1]["result"].(map[string]any)
	if !ok || busy["ok"] != false || busy["error_kind"] != "resource_busy" || busy["capacity"] != 1 {
		t.Fatalf("busy result is not nested typed DTO: %#v", items[1])
	}
}

func TestGitAndGitHubFailedTwoStreamOutputKeepsBoundedEvidence(t *testing.T) {
	bin := t.TempDir()
	script := `#!/bin/sh
printf 'OUT_HEAD:'
i=0; while [ "$i" -lt 600 ]; do printf 'o'; i=$((i+1)); done
printf ':OUT_TAIL'
printf 'ERR_HEAD:' >&2
i=0; while [ "$i" -lt 600 ]; do printf 'e' >&2; i=$((i+1)); done
printf ':ERR_TAIL' >&2
exit 7
`
	for _, name := range []string{"git", "gh"} {
		if err := os.WriteFile(filepath.Join(bin, name), []byte(script), 0o700); err != nil {
			t.Fatal(err)
		}
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	engine, root := newTestEngine(t)
	engine.settings.DefaultInlineOutputBytes = 256
	engine.settings.MaxResponseChars = 256

	gitResult := engine.gitOutputCapped(context.Background(), root, "git_test", 256, "status")
	assertFailedTwoStreamArtifact(t, engine, gitResult, "git_error", 256)
	ghResult := engine.runGH(context.Background(), root, "api", "test")
	assertFailedTwoStreamArtifact(t, engine, ghResult, "gh_error", 256)
}

func TestGitHubJSONFailsClosedWhenTwoStreamPreviewIsIncomplete(t *testing.T) {
	bin := t.TempDir()
	script := `#!/bin/sh
printf '{"items":"JSON_HEAD:'
i=0; while [ "$i" -lt 600 ]; do printf 'j'; i=$((i+1)); done
printf ':JSON_TAIL"}'
printf 'WARN_HEAD:' >&2
i=0; while [ "$i" -lt 600 ]; do printf 'w' >&2; i=$((i+1)); done
printf ':WARN_TAIL' >&2
`
	if err := os.WriteFile(filepath.Join(bin, "gh"), []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	engine, root := newTestEngine(t)
	engine.settings.DefaultInlineOutputBytes = 256
	engine.settings.MaxResponseChars = 256

	result := engine.runGHJSON(context.Background(), root, "api", "test")
	if result["ok"] != false || result["error_kind"] != "gh_json_incomplete" || result["artifact"] == nil || result["receipt"] == nil || result["continuation"] == nil {
		t.Fatalf("truncated JSON was not rejected with durable evidence: %#v", result)
	}
}

func assertFailedTwoStreamArtifact(t *testing.T, engine *Engine, result map[string]any, errorKind string, limit int) {
	t.Helper()
	if result["ok"] != false || result["error_kind"] != errorKind || result["truncated"] != true {
		t.Fatalf("unexpected failed result: %#v", result)
	}
	output, _ := result["output"].(string)
	for _, marker := range []string{"OUT_HEAD:", ":OUT_TAIL", "ERR_HEAD:", ":ERR_TAIL"} {
		if !strings.Contains(output, marker) {
			t.Fatalf("combined preview lost %q: %q", marker, output)
		}
	}
	if len(output) > limit {
		t.Fatalf("combined preview exceeded limit: %d > %d", len(output), limit)
	}
	receipt := result["receipt"].(map[string]any)
	returned := receipt["returned"].(map[string]any)
	total := receipt["total"].(map[string]any)
	if returned["bytes"] != int64(len(output)) || returned["stdout_bytes"].(int) == 0 || returned["stderr_bytes"].(int) == 0 {
		t.Fatalf("returned receipt is not truthful: %#v", receipt)
	}
	if total["stdout_bytes"].(int64) <= int64(returned["stdout_bytes"].(int)) || total["stderr_bytes"].(int64) <= int64(returned["stderr_bytes"].(int)) {
		t.Fatalf("total receipt is not truthful: %#v", receipt)
	}
	continuation, ok := result["continuation"].(map[string]any)
	if !ok || continuation["tool"] != "read_artifact" {
		t.Fatalf("failed output has no continuation: %#v", result)
	}
	artifactID := result["artifact"].(map[string]any)["artifact_id"]
	page := engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": artifactID, "max_bytes": 4096})
	if page["ok"] != true || page["metadata"].(map[string]any)["sha256"] == nil {
		t.Fatalf("failed output artifact is unavailable: %#v", page)
	}
	records := page["payload"].(map[string]any)["records"].([]map[string]any)
	var full strings.Builder
	for _, record := range records {
		full.WriteString(record["data"].(string))
	}
	for _, marker := range []string{"OUT_HEAD:", ":OUT_TAIL", "ERR_HEAD:", ":ERR_TAIL"} {
		if !strings.Contains(full.String(), marker) {
			t.Fatalf("artifact lost %q: %#v", marker, page)
		}
	}
}

func TestLargeGitOutputIsBoundedAndDurablyContinuable(t *testing.T) {
	bin := t.TempDir()
	git := filepath.Join(bin, "git")
	if err := os.WriteFile(git, []byte("#!/bin/sh\nprintf 'git-head-😀\\n'; yes git-output | head -c 524288; printf '\\ngit-tail-😀\\n'\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	engine, root := newTestEngine(t)
	result := engine.gitOutputCapped(context.Background(), root, "git_diff", 128, "diff")
	output := result["output"].(string)
	if result["ok"] != true || result["truncated"] != true || len(output) > 128 || !strings.Contains(output, "git-head-😀") || !strings.Contains(output, "git-tail-😀") || !utf8.ValidString(output) {
		t.Fatalf("git output was not bounded: %#v", result)
	}
	id := result["artifact"].(map[string]any)["artifact_id"].(string)
	store, err := engine.artifactStore()
	if err != nil {
		t.Fatal(err)
	}
	page, err := store.readPage(id, "text", "", 64*1024)
	if err != nil {
		t.Fatal(err)
	}
	if page["has_more"] != true || page["payload"].(map[string]any)["type"] != "text" || !strings.Contains(page["payload"].(map[string]any)["text"].(string), "git-output") {
		t.Fatalf("durable continuation missing: %#v", page)
	}
}

func TestTruncatedRunCommandReturnsCanonicalContinuation(t *testing.T) {
	engine, _ := newTestEngine(t)
	result := engine.runCommand(context.Background(), commandRequest{Command: "printf 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'", Timeout: 5 * time.Second, CWD: ".", MaxOutput: 16, TailLines: -1, ParseKind: "none", PolicyExempt: true})
	if result["ok"] != true || result["output_truncated"] != true {
		t.Fatalf("command did not truncate as expected: %#v", result)
	}
	continuation, ok := result["continuation"].(map[string]any)
	if !ok || continuation["tool"] != "read_artifact" {
		t.Fatalf("missing canonical continuation: %#v", result)
	}
	arguments := continuation["arguments"].(map[string]any)
	if arguments["artifact_id"] != result["log_id"] {
		t.Fatalf("continuation artifact mismatch: %#v", continuation)
	}
	receipt := result["receipt"].(map[string]any)
	if receipt["status"] != "partial" || receipt["completeness"] != "partial" || receipt["reason"] != "inline_limit" {
		t.Fatalf("truncated receipt drift: %#v", receipt)
	}
	page := engine.Execute(context.Background(), "read_artifact", map[string]any{"artifact_id": result["log_id"]})
	if page["ok"] != true {
		t.Fatalf("public artifact read failed: %#v", page)
	}
	payload := page["payload"].(map[string]any)
	if payload["type"] != "records" || payload["records"] == nil {
		t.Fatalf("public artifact read did not infer records: %#v", page)
	}
	if _, legacy := page["data"]; legacy {
		t.Fatalf("public artifact read returned legacy data: %#v", page)
	}
}
