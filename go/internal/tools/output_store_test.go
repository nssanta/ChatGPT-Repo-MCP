package tools

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"sync"
	"testing"
	"unicode/utf8"
)

func TestStreamCaptureRedactsAcrossEveryChunkBoundary(t *testing.T) {
	secret := "prefix token=super-secret-value safe-suffix\n"
	expected := "prefix token=<redacted> safe-suffix\n"
	for split := 1; split < len(secret); split++ {
		path := filepath.Join(t.TempDir(), "capture")
		capture, err := newStreamCapture(path, 1024, 1024)
		if err != nil {
			t.Fatal(err)
		}
		_, _ = capture.Write([]byte(secret[:split]))
		_, _ = capture.Write([]byte(secret[split:]))
		if err := capture.Close(); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), "super-secret-value") {
			t.Fatalf("split %d leaked secret: %q", split, data)
		}
		if string(data) != expected {
			t.Fatalf("split %d placeholder/suffix drift: got=%q want=%q", split, data, expected)
		}
	}
}

func TestStreamCaptureRedactsPrivateKeyAcrossChunks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 1024, 1024)
	if err != nil {
		t.Fatal(err)
	}
	parts := []string{"before\n-----BE", "GIN PRIVATE KEY-----\nSECRET", "-BODY\n-----END PRIVATE KEY-----\nafter\n"}
	for _, part := range parts {
		if _, err := capture.Write([]byte(part)); err != nil {
			t.Fatal(err)
		}
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	if strings.Contains(string(data), "SECRET-BODY") || !strings.Contains(string(data), "[REDACTED PRIVATE KEY]") {
		t.Fatalf("unexpected redaction: %q", data)
	}
}

func TestOrdinaryRedactionPlaceholderParityAndSuffix(t *testing.T) {
	input := "Bearer abc.def-123 safe token=secret-value tail ghp_abcdefghijklmnopqrstuvwxyz"
	got := redact(input)
	want := "Bearer <redacted> safe token=<redacted> tail <redacted>"
	if got != want {
		t.Fatalf("redaction placeholder drift: got=%q want=%q", got, want)
	}
}

func TestStreamCaptureBoundsMemoryAndKeepsDurableOutput(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 128, 128)
	if err != nil {
		t.Fatal(err)
	}
	chunk := []byte(strings.Repeat("x", 64*1024-1) + "\n")
	const total = 64 * 1024 * 1024
	runtime.GC()
	var before, current runtime.MemStats
	runtime.ReadMemStats(&before)
	peak := before.HeapAlloc
	for written := 0; written < total; written += len(chunk) {
		if _, err := capture.Write(chunk); err != nil {
			t.Fatal(err)
		}
		runtime.ReadMemStats(&current)
		if current.HeapAlloc > peak {
			peak = current.HeapAlloc
		}
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	if len(capture.head) > 128 || len(capture.tail) > 128 {
		t.Fatalf("unbounded memory: head=%d tail=%d", len(capture.head), len(capture.tail))
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	if info.Size() != total {
		t.Fatalf("durable output mismatch: %d != %d", info.Size(), total)
	}
	if peak-before.HeapAlloc > 32*1024*1024 {
		t.Fatalf("capture heap grew by %d bytes", peak-before.HeapAlloc)
	}
}

func TestConcurrentStreamCapturesStayWithinSharedMemoryBudget(t *testing.T) {
	runtime.GC()
	var before, after runtime.MemStats
	runtime.ReadMemStats(&before)
	chunk := []byte(strings.Repeat("c", 64*1024-1) + "\n")
	var wait sync.WaitGroup
	for worker := 0; worker < 4; worker++ {
		wait.Add(1)
		go func(worker int) {
			defer wait.Done()
			capture, err := newStreamCapture(filepath.Join(t.TempDir(), fmt.Sprintf("capture-%d", worker)), 128, 128)
			if err != nil {
				t.Error(err)
				return
			}
			for written := 0; written < 16*1024*1024; written += len(chunk) {
				if _, err := capture.Write(chunk); err != nil {
					t.Error(err)
					return
				}
			}
			if err := capture.Close(); err != nil {
				t.Error(err)
			}
		}(worker)
	}
	wait.Wait()
	runtime.ReadMemStats(&after)
	if after.HeapAlloc > before.HeapAlloc+64*1024*1024 {
		t.Fatalf("concurrent capture heap grew by %d bytes", after.HeapAlloc-before.HeapAlloc)
	}
}

func TestStreamCapturePreservesHugeRecordExactly(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 128, 128)
	if err != nil {
		t.Fatal(err)
	}
	payload := strings.Repeat("ordinary-output-", 20_000) + "\n"
	for offset := 0; offset < len(payload); offset += 997 {
		end := min(offset+997, len(payload))
		if _, err := capture.Write([]byte(payload[offset:end])); err != nil {
			t.Fatal(err)
		}
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != payload {
		t.Fatalf("huge ordinary record changed: got=%d want=%d", len(data), len(payload))
	}
	if len(capture.pending) > maxOutputRecordBytes {
		t.Fatalf("pending memory is unbounded: %d", len(capture.pending))
	}
}

func TestStreamCaptureFailsClosedForHugeSecretRecord(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 128, 128)
	if err != nil {
		t.Fatal(err)
	}
	secret := "token=" + strings.Repeat("s", maxOutputRecordBytes*2) + "\nnext\n"
	if _, err := capture.Write([]byte(secret)); err != nil {
		t.Fatal(err)
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), strings.Repeat("s", 100)) {
		t.Fatalf("secret suffix leaked: %q", data)
	}
	if !strings.Contains(string(data), "<redacted>") || !strings.HasSuffix(string(data), "\nnext\n") {
		t.Fatalf("unexpected fail-closed output: %q", data)
	}
}

func TestStreamCaptureFailsClosedForHugePrivateKeyByteChunks(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 128, 128)
	if err != nil {
		t.Fatal(err)
	}
	payload := "prefix " + "-----BEGIN PRIVATE KEY-----" + strings.Repeat("K", maxOutputRecordBytes*2) + "-----END PRIVATE KEY-----\n"
	for index := range payload {
		if _, err := capture.Write([]byte(payload[index : index+1])); err != nil {
			t.Fatal(err)
		}
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(data), strings.Repeat("K", 100)) || !strings.Contains(string(data), "[REDACTED OVERSIZED SECRET RECORD]") {
		t.Fatalf("private key leaked: %q", data)
	}
}

func TestStreamCapturePreviewDoesNotSplitUTF8(t *testing.T) {
	path := filepath.Join(t.TempDir(), "capture")
	capture, err := newStreamCapture(path, 5, 5)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := capture.Write([]byte("🙂🙂\n")); err != nil {
		t.Fatal(err)
	}
	if err := capture.Close(); err != nil {
		t.Fatal(err)
	}
	if !utf8.ValidString(capture.Head()) || !utf8.ValidString(capture.TailLines(1)) {
		t.Fatalf("invalid UTF-8 preview: head=%q tail=%q", capture.Head(), capture.TailLines(1))
	}
}

func TestBoundedLogPagePaginatesWithoutWholeFileResult(t *testing.T) {
	path := filepath.Join(t.TempDir(), "log")
	if err := os.WriteFile(path, []byte(strings.Repeat("0123456789\n", 1000)), 0o600); err != nil {
		t.Fatal(err)
	}
	content, first, last, total, truncated, err := boundedLogPage(path, 1, 0, 100, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(content) > 100 || first != 1 || last == 0 || total != 1000 || !truncated {
		t.Fatalf("bad page: bytes=%d first=%d last=%d total=%d truncated=%v", len(content), first, last, total, truncated)
	}
}
