//go:build !windows

package tools

import (
	"context"
	"fmt"
	"testing"
	"time"
)

const regressionInlineLimit = 64 * 1024

func TestForegroundTailLinesTrailingNewlineDoesNotClaimTruncation(t *testing.T) {
	engine := newTruncationRegressionEngine(t)
	result := engine.runCommand(context.Background(), commandRequest{
		Command:      "printf 'stdout-line\\n'; printf 'stderr-line\\n' >&2",
		Timeout:      5 * time.Second,
		CWD:          ".",
		MaxOutput:    regressionInlineLimit,
		TailLines:    200,
		ParseKind:    "none",
		PolicyExempt: true,
	})

	assertTrailingNewlineResultIsComplete(t, result, "stdout-line", "stderr-line")
}

func TestBackgroundTailLinesTrailingNewlineDoesNotClaimTruncation(t *testing.T) {
	engine := newTruncationRegressionEngine(t)
	started := engine.startInternalJob(context.Background(), map[string]any{
		"command":          "printf 'stdout-line\\n'; printf 'stderr-line\\n' >&2",
		"timeout_ms":       5_000,
		"max_output_chars": regressionInlineLimit,
		"tail_lines":       200,
	})
	if started["ok"] != true {
		t.Fatalf("background command did not start: %#v", started)
	}
	jobID := started["job_id"].(string)
	entry := engine.jobs[jobID]
	select {
	case <-entry.done:
	case <-time.After(10 * time.Second):
		t.Fatal("background command did not finish")
	}

	result := engine.getJob(jobID, 200, false)
	assertTrailingNewlineResultIsComplete(t, result, "stdout-line", "stderr-line")
}

func TestTailLinesOutputAboveInlineLimitRemainsTruncated(t *testing.T) {
	for _, background := range []bool{false, true} {
		for _, stream := range []string{"stdout", "stderr"} {
			t.Run(fmt.Sprintf("background=%t/%s", background, stream), func(t *testing.T) {
				engine := newTruncationRegressionEngine(t)
				command := "printf '%070000d' 0"
				if stream == "stderr" {
					command += " >&2"
				}

				result := runTruncationRegressionCommand(t, engine, command, background)
				if result["ok"] != true || result["output_truncated"] != true {
					t.Fatalf("over-budget %s output did not report truncation: %#v", stream, result)
				}
				continuation, ok := result["continuation"].(map[string]any)
				if !ok || continuation["tool"] != "read_artifact" {
					t.Fatalf("over-budget %s output has no artifact continuation: %#v", stream, result)
				}
				receipt := result["receipt"].(map[string]any)
				if receipt["status"] != "partial" || receipt["completeness"] != "partial" || receipt["reason"] != "inline_limit" {
					t.Fatalf("over-budget %s output has dishonest receipt: %#v", stream, receipt)
				}
				if result[stream+"_bytes"] != int64(70_000) {
					t.Fatalf("over-budget %s total changed: %#v", stream, result)
				}
				visible := result[stream].(string)
				if len(visible) == 0 || len(visible) > regressionInlineLimit {
					t.Fatalf("over-budget %s preview is not bounded: bytes=%d", stream, len(visible))
				}
			})
		}
	}
}

func TestBackgroundCombinedStreamsShareInlineBudget(t *testing.T) {
	engine := newTruncationRegressionEngine(t)
	result := runTruncationRegressionCommand(
		t,
		engine,
		"printf '%040960d' 0; printf '%040960d' 0 >&2",
		true,
	)

	stdout, stderr := result["stdout"].(string), result["stderr"].(string)
	if len(stdout)+len(stderr) > regressionInlineLimit {
		t.Fatalf("background streams exceeded shared inline budget: stdout=%d stderr=%d result=%#v", len(stdout), len(stderr), result)
	}
	if result["output_truncated"] != true {
		t.Fatalf("over-budget combined output did not report truncation: %#v", result)
	}
	if result["stdout_bytes"] != int64(40_960) || result["stderr_bytes"] != int64(40_960) {
		t.Fatalf("combined stream totals changed: %#v", result)
	}
	continuation, ok := result["continuation"].(map[string]any)
	if !ok || continuation["tool"] != "read_artifact" {
		t.Fatalf("over-budget combined output has no continuation: %#v", result)
	}
	receipt := result["receipt"].(map[string]any)
	if receipt["status"] != "partial" || receipt["completeness"] != "partial" || receipt["reason"] != "inline_limit" {
		t.Fatalf("over-budget combined output has dishonest receipt: %#v", receipt)
	}
}

func TestBackgroundCombinedStreamsRespectSingleByteInlineBudget(t *testing.T) {
	engine := newTruncationRegressionEngine(t)
	started := engine.startInternalJob(context.Background(), map[string]any{
		"command":          "printf x; printf y >&2",
		"timeout_ms":       5_000,
		"max_output_chars": 1,
		"tail_lines":       200,
	})
	if started["ok"] != true {
		t.Fatalf("background command did not start: %#v", started)
	}
	jobID := started["job_id"].(string)
	entry := engine.jobs[jobID]
	select {
	case <-entry.done:
	case <-time.After(10 * time.Second):
		t.Fatal("background command did not finish")
	}

	result := engine.getJob(jobID, 200, false)
	stdout, stderr := result["stdout"].(string), result["stderr"].(string)
	if len(stdout)+len(stderr) > 1 {
		t.Fatalf("background streams exceeded single-byte inline budget: stdout=%q stderr=%q result=%#v", stdout, stderr, result)
	}
	if result["output_truncated"] != true {
		t.Fatalf("single-byte combined output did not report truncation: %#v", result)
	}
	if result["stdout_bytes"] != int64(1) || result["stderr_bytes"] != int64(1) {
		t.Fatalf("single-byte combined stream totals changed: %#v", result)
	}
	continuation, ok := result["continuation"].(map[string]any)
	if !ok || continuation["tool"] != "read_artifact" {
		t.Fatalf("single-byte combined output has no continuation: %#v", result)
	}
	receipt := result["receipt"].(map[string]any)
	if receipt["status"] != "partial" || receipt["completeness"] != "partial" || receipt["reason"] != "inline_limit" {
		t.Fatalf("single-byte combined output has dishonest receipt: %#v", receipt)
	}
}

func runTruncationRegressionCommand(t *testing.T, engine *Engine, command string, background bool) map[string]any {
	t.Helper()
	if !background {
		return engine.runCommand(context.Background(), commandRequest{
			Command:      command,
			Timeout:      5 * time.Second,
			CWD:          ".",
			MaxOutput:    regressionInlineLimit,
			TailLines:    200,
			ParseKind:    "none",
			PolicyExempt: true,
		})
	}

	started := engine.startInternalJob(context.Background(), map[string]any{
		"command":          command,
		"timeout_ms":       5_000,
		"max_output_chars": regressionInlineLimit,
		"tail_lines":       200,
	})
	if started["ok"] != true {
		t.Fatalf("background command did not start: %#v", started)
	}
	jobID := started["job_id"].(string)
	entry := engine.jobs[jobID]
	select {
	case <-entry.done:
	case <-time.After(10 * time.Second):
		t.Fatal("background command did not finish")
	}
	return engine.getJob(jobID, 200, false)
}

func newTruncationRegressionEngine(t *testing.T) *Engine {
	t.Helper()
	return New(testSettings(t.TempDir()), nil)
}

func assertTrailingNewlineResultIsComplete(t *testing.T, result map[string]any, stdout, stderr string) {
	t.Helper()
	if result["ok"] != true {
		t.Fatalf("command failed: %#v", result)
	}
	if result["stdout"] != stdout || result["stderr"] != stderr {
		t.Fatalf("unexpected tail output: %#v", result)
	}
	if result["output_truncated"] != false {
		t.Fatalf("removed trailing newline was reported as truncation: %#v", result)
	}
	if _, exists := result["continuation"]; exists {
		t.Fatalf("complete output exposed a continuation: %#v", result)
	}
	receipt := result["receipt"].(map[string]any)
	if receipt["status"] != "completed" || receipt["completeness"] != "complete" || receipt["reason"] != "none" {
		t.Fatalf("complete output has partial receipt: %#v", receipt)
	}
}
