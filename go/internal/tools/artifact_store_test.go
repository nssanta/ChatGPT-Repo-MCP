package tools

import (
	"crypto/sha256"
	"encoding/base64"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestArtifactStoreRejectsPerArtifactQuota(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 1024, 32, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("one")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte(strings.Repeat("x", 33))); err == nil || !strings.Contains(err.Error(), "artifact_quota") {
		t.Fatalf("expected typed quota error, got %v", err)
	}
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
}

func TestArtifactStoreEvictsExpiredAndNeverActive(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 200, 64, 10*time.Millisecond, 0)
	if err != nil {
		t.Fatal(err)
	}
	expired, err := store.create("expired")
	if err != nil {
		t.Fatal(err)
	}
	if err := expired.WriteRecord(1, []byte(strings.Repeat("e", 20))); err != nil {
		t.Fatal(err)
	}
	if err := expired.Close(); err != nil {
		t.Fatal(err)
	}
	expiredPath := filepath.Join(root, "artifacts", "expired.records")
	old := time.Now().Add(-time.Hour)
	if err := os.Chtimes(expiredPath, old, old); err != nil {
		t.Fatal(err)
	}
	active, err := store.create("active")
	if err != nil {
		t.Fatal(err)
	}
	if err := active.WriteRecord(1, []byte(strings.Repeat("a", 20))); err != nil {
		t.Fatal(err)
	}
	newest, err := store.create("new")
	if err != nil {
		t.Fatal(err)
	}
	if err := newest.WriteRecord(1, []byte(strings.Repeat("n", 20))); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(expiredPath); !os.IsNotExist(err) {
		t.Fatalf("expired artifact was not evicted: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "artifacts", "active.records")); err != nil {
		t.Fatalf("active artifact was evicted: %v", err)
	}
	if err := active.Close(); err != nil {
		t.Fatal(err)
	}
	if err := newest.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestArtifactRestartAccountsAndExpiresCompanionOnlyGroups(t *testing.T) {
	root := t.TempDir()
	if err := os.MkdirAll(filepath.Join(root, "artifacts"), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "logs"), 0o700); err != nil {
		t.Fatal(err)
	}
	paths := []string{
		filepath.Join(root, "command-orphan.stdout"),
		filepath.Join(root, "command-orphan.stderr"),
		filepath.Join(root, "logs", "terminal-orphan.json"),
		filepath.Join(root, "logs", "terminal-orphan.combined"),
	}
	wantUsage := int64(0)
	for _, path := range paths {
		content := []byte(filepath.Base(path))
		if err := os.WriteFile(path, content, 0o600); err != nil {
			t.Fatal(err)
		}
		wantUsage += int64(len(content))
	}
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	if store.usage != wantUsage || store.artifactUse["command-orphan.records"] == 0 || store.artifactUse["terminal-orphan.records"] == 0 {
		t.Fatalf("restart missed companion-only groups: usage=%d want=%d groups=%#v", store.usage, wantUsage, store.artifactUse)
	}
	old := time.Now().Add(-time.Hour)
	for _, path := range paths {
		if err := os.Chtimes(path, old, old); err != nil {
			t.Fatal(err)
		}
	}
	restarted, err := newArtifactStore(root, 4096, 2048, time.Millisecond, 0)
	if err != nil {
		t.Fatal(err)
	}
	if restarted.usage != 0 {
		t.Fatalf("expired companion-only groups remained accounted: %d", restarted.usage)
	}
	for _, path := range paths {
		if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("expired orphan companion was not removed: %s: %v", path, err)
		}
	}
}

func TestCommandCaptureCreateFailureLeavesNoCompanionOnlyFiles(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	active, err := store.create("duplicate")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := newCommandCapture(root, "duplicate", 64, store); err == nil {
		t.Fatal("expected duplicate active artifact creation to fail")
	}
	for _, suffix := range []string{".stdout", ".stderr"} {
		if _, err := os.Stat(filepath.Join(root, "duplicate"+suffix)); !errors.Is(err, os.ErrNotExist) {
			t.Fatalf("failed capture left orphan companion %s: %v", suffix, err)
		}
	}
	if err := active.Abort(); err != nil {
		t.Fatal(err)
	}
}

func TestArtifactLogicalQuotaDoesNotDoubleCountPhysicalCompanion(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 20, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("logical-boundary")
	if err != nil {
		t.Fatal(err)
	}
	writer.companionCopies = 1
	if err := writer.WriteRecord(1, []byte(strings.Repeat("x", 20))); err != nil {
		t.Fatalf("logical payload at item limit was rejected: %v", err)
	}
	if got, want := store.usage, int64(9+20+20); got != want {
		t.Fatalf("physical usage lost frame or companion bytes: got %d want %d", got, want)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
}

func TestArtifactCompanionAccountingMatchesDiskAcrossRestart(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("companions")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("payload")); err != nil {
		t.Fatal(err)
	}
	metadataPath := filepath.Join(root, "companions.json")
	if err := store.writeCompanion("companions", metadataPath, []byte("metadata\n")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	physical := int64(0)
	for _, path := range append([]string{filepath.Join(root, "artifacts", "companions.records")}, store.companionPaths("companions")...) {
		if info, statErr := os.Stat(path); statErr == nil {
			physical += info.Size()
		}
	}
	if store.usage != physical || store.artifactUse["companions.records"] != physical {
		t.Fatalf("live accounting differs from disk: usage=%d item=%d disk=%d", store.usage, store.artifactUse["companions.records"], physical)
	}
	restarted, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	if restarted.usage != physical || restarted.artifactUse["companions.records"] != physical {
		t.Fatalf("restart accounting differs from disk: usage=%d item=%d disk=%d", restarted.usage, restarted.artifactUse["companions.records"], physical)
	}
}

func TestArtifactCompanionOverwriteReservesCoexistingTempBytes(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("overwrite")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("payload")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(root, "overwrite.json")
	if err := store.writeCompanion("overwrite", path, []byte("old-data")); err != nil {
		t.Fatal(err)
	}
	steadyUsage := store.usage
	store.totalLimit = steadyUsage + int64(len("new-data")) - 1
	if err := store.writeCompanion("overwrite", path, []byte("new-data")); err == nil || !strings.Contains(err.Error(), "artifact_quota") {
		t.Fatalf("overwrite ignored transient temp-file quota: %v", err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != "old-data" || store.usage != steadyUsage {
		t.Fatalf("failed overwrite changed target/accounting: data=%q usage=%d want=%d", data, store.usage, steadyUsage)
	}
	store.totalLimit = steadyUsage + int64(len("new-data"))
	if err := store.writeCompanion("overwrite", path, []byte("new-data")); err != nil {
		t.Fatal(err)
	}
	if store.usage != steadyUsage {
		t.Fatalf("successful same-size overwrite drifted steady accounting: got=%d want=%d", store.usage, steadyUsage)
	}
}

func TestArtifactReadPreservesCreationAndRefreshesReportedExpiry(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("recency")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("payload")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	first, err := store.readPage("recency", "text", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(2 * time.Millisecond)
	second, err := store.readPage("recency", "text", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	firstMetadata := first["metadata"].(map[string]any)
	secondMetadata := second["metadata"].(map[string]any)
	if firstMetadata["created_at"] != secondMetadata["created_at"] {
		t.Fatalf("artifact creation time changed on read: first=%v second=%v", firstMetadata["created_at"], secondMetadata["created_at"])
	}
	firstExpiry, err := time.Parse(time.RFC3339Nano, firstMetadata["expires_at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	secondExpiry, err := time.Parse(time.RFC3339Nano, secondMetadata["expires_at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	if !secondExpiry.After(firstExpiry) {
		t.Fatalf("reported expiry did not follow refreshed LRU access: first=%s second=%s", firstExpiry, secondExpiry)
	}
}

func TestArtifactContinuationDoesNotRescanConsumedPrefix(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("seek-page")
	if err != nil {
		t.Fatal(err)
	}
	for _, value := range []string{"aaaa", "bbbb", "cccc"} {
		if err := writer.WriteRecord(1, []byte(value)); err != nil {
			t.Fatal(err)
		}
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	first, err := store.readPage("seek-page", "text", "", 4)
	if err != nil {
		t.Fatal(err)
	}
	recordsPath := filepath.Join(root, "artifacts", "seek-page.records")
	file, err := os.OpenFile(recordsPath, os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := file.WriteAt([]byte{0xff}, 0); err != nil {
		t.Fatal(err)
	}
	_ = file.Close()
	continued, err := store.readPage("seek-page", "text", first["next_cursor"].(string), 8)
	if err != nil {
		t.Fatalf("continuation rescanned corrupt consumed prefix: %v", err)
	}
	if got := continued["payload"].(map[string]any)["text"]; got != "bbbbcccc" {
		t.Fatalf("unexpected continuation payload: %q", got)
	}
}

func TestActiveArtifactReadDoesNotWaitForWriterLock(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("nonblocking")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("ready")); err != nil {
		t.Fatal(err)
	}
	writer.ioLock.Lock()
	done := make(chan error, 1)
	go func() {
		_, readErr := store.readPage("nonblocking", "text", "", 64)
		done <- readErr
	}()
	select {
	case readErr := <-done:
		if readErr != nil {
			t.Fatal(readErr)
		}
	case <-time.After(500 * time.Millisecond):
		t.Fatal("active artifact read blocked on writer lock")
	}
	writer.ioLock.Unlock()
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
}

func TestArtifactReadPageIsBoundedOpaqueAndStdoutFirst(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("run-1")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(2, []byte("stderr-last")); err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("stdout-first")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	page, err := store.readPage("run-1", "records", "", 6)
	if err != nil {
		t.Fatal(err)
	}
	logical := []byte("stdout-firststderr-last")
	digest := sha256.Sum256(logical)
	metadata := page["metadata"].(map[string]any)
	if metadata["size_bytes"] != int64(len(logical)) || metadata["sha256"] != fmt.Sprintf("%x", digest[:]) {
		t.Fatalf("metadata describes physical frames instead of logical payload: %#v", metadata)
	}
	receipt := page["receipt"].(map[string]any)
	if receipt["status"] != "partial" || receipt["reason"] != "inline_limit" || receipt["total"].(map[string]any)["bytes"] != int64(len(logical)) {
		t.Fatalf("partial receipt does not describe logical payload: %#v", receipt)
	}
	if _, legacy := page["data"]; legacy {
		t.Fatalf("legacy top-level data conflicts with typed schema: %#v", page)
	}
	typedPayload := page["payload"].(map[string]any)
	if typedPayload["type"] != "records" {
		t.Fatalf("unexpected typed payload: %#v", typedPayload)
	}
	records := typedPayload["records"].([]map[string]any)
	if len(records) != 1 || records[0]["stream"] != "stdout" || records[0]["data"] != "stdout" {
		t.Fatalf("unexpected first page: %#v", page)
	}
	cursor, ok := page["next_cursor"].(string)
	if !ok || cursor == "" || strings.Contains(cursor, "run-1") {
		t.Fatalf("cursor is not opaque: %#v", page["next_cursor"])
	}
	restarted, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	continued, err := restarted.readPage("run-1", "records", cursor, 64)
	if err != nil {
		t.Fatal(err)
	}
	continuedPayload := continued["payload"].(map[string]any)
	continuedRecords := continuedPayload["records"].([]map[string]any)
	if continued["byte_range"].(map[string]any)["start"] != int64(6) {
		t.Fatalf("continuation byte range restarted: %#v", continued["byte_range"])
	}
	if len(continuedRecords) < 2 || continuedRecords[len(continuedRecords)-1]["stream"] != "stderr" {
		t.Fatalf("stdout/stderr ordering lost: %#v", continued)
	}
	if _, err := store.readPage("different", "records", cursor, 64); err == nil {
		t.Fatal("cursor replay across artifacts must fail")
	}
	binaryPage, err := restarted.readPage("run-1", "base64", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	binaryPayload := binaryPage["payload"].(map[string]any)
	decoded, err := base64.StdEncoding.DecodeString(binaryPayload["base64"].(string))
	if err != nil {
		t.Fatal(err)
	}
	if binaryPayload["type"] != "base64" || string(decoded) != "stdout-firststderr-last" {
		t.Fatalf("bad typed base64 payload: %#v", binaryPayload)
	}
	if _, legacy := binaryPage["data"]; legacy {
		t.Fatalf("base64 page leaked legacy data field: %#v", binaryPage)
	}
}

func TestArtifactWriteIOFailureRollsBackAccounting(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("fault")
	if err != nil {
		t.Fatal(err)
	}
	before := store.usage
	if err := writer.file.Close(); err != nil {
		t.Fatal(err)
	}
	err = writer.WriteRecord(1, []byte("must-not-fallback-to-memory"))
	if err == nil || !strings.Contains(err.Error(), "artifact_io") {
		t.Fatalf("expected typed artifact_io, got %v", err)
	}
	if store.usage != before || store.artifactUse["fault.records"] != 0 {
		t.Fatalf("accounting not rolled back: usage=%d item=%d", store.usage, store.artifactUse["fault.records"])
	}
	_ = writer.Close()
	page, readErr := store.readPage("fault", "records", "", 64)
	if readErr != nil {
		t.Fatal(readErr)
	}
	if page["eof"] != false || page["has_more"] != true || page["metadata"].(map[string]any)["sha256"] != nil || page["receipt"].(map[string]any)["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("failed close upgraded artifact truth: %#v", page)
	}
}

func TestArtifactTextPagesPreserveUTF8BoundariesAndEOF(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("utf8")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("🙂🙂")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	first, err := store.readPage("utf8", "text", "", 5)
	if err != nil {
		t.Fatal(err)
	}
	if first["payload"].(map[string]any)["text"] != "🙂" || first["payload"].(map[string]any)["type"] != "text" || first["has_more"] != true || first["eof"] != false {
		t.Fatalf("bad first UTF-8 page: %#v", first)
	}
	second, err := store.readPage("utf8", "text", first["next_cursor"].(string), 5)
	if err != nil {
		t.Fatal(err)
	}
	if second["payload"].(map[string]any)["text"] != "🙂" || second["has_more"] != false || second["eof"] != true || second["next_cursor"] != nil {
		t.Fatalf("bad terminal UTF-8 page: %#v", second)
	}
}

func TestLegacyCommandLogsShareArtifactQuotaAndEviction(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 256, 128, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	capture, err := newCommandCapture(root, "legacy", 32, store)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := capture.stdout.Write([]byte(strings.Repeat("x", 129) + "\n")); err == nil || !strings.Contains(err.Error(), "artifact_quota") {
		t.Fatalf("legacy output bypassed artifact quota: %v", err)
	}
	info, statErr := os.Stat(filepath.Join(root, "legacy.stdout"))
	if statErr != nil {
		t.Fatal(statErr)
	}
	if info.Size() != 0 {
		t.Fatalf("legacy bytes persisted after quota rejection: %d", info.Size())
	}
	_ = capture.Close()
}

func TestActiveArtifactContinuationWaitsForGrowthAndFinalTruth(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("active-growth")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(2, []byte("stderr-held")); err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("stdout-one")); err != nil {
		t.Fatal(err)
	}
	first, err := store.readPage("active-growth", "records", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	firstPayload := first["payload"].(map[string]any)["records"].([]map[string]any)
	if len(firstPayload) != 1 || firstPayload[0]["data"] != "stdout-one" {
		t.Fatalf("active page exposed wrong ordering: %#v", first)
	}
	if first["eof"] != false || first["has_more"] != true || first["next_cursor"] == nil || first["metadata"].(map[string]any)["sha256"] != nil {
		t.Fatalf("active artifact claimed final truth: %#v", first)
	}
	firstReceipt := first["receipt"].(map[string]any)
	if firstReceipt["status"] != "partial" || firstReceipt["completeness"] != "partial" || firstReceipt["reason"] != "inline_limit" || firstReceipt["applied"].(map[string]any)["source_complete"] != false || len(firstReceipt["warnings"].([]string)) == 0 {
		t.Fatalf("active receipt drift: %#v", firstReceipt)
	}
	if err := writer.WriteRecord(1, []byte("stdout-two")); err != nil {
		t.Fatal(err)
	}
	second, err := store.readPage("active-growth", "records", first["next_cursor"].(string), 64)
	if err != nil {
		t.Fatal(err)
	}
	secondRecords := second["payload"].(map[string]any)["records"].([]map[string]any)
	if len(secondRecords) != 1 || secondRecords[0]["data"] != "stdout-two" || second["eof"] != false {
		t.Fatalf("active growth continuation failed: %#v", second)
	}
	if err := writer.Close(); err != nil {
		t.Fatal(err)
	}
	final, err := store.readPage("active-growth", "records", second["next_cursor"].(string), 64)
	if err != nil {
		t.Fatal(err)
	}
	finalRecords := final["payload"].(map[string]any)["records"].([]map[string]any)
	if len(finalRecords) != 1 || finalRecords[0]["stream"] != "stderr" || finalRecords[0]["data"] != "stderr-held" || final["eof"] != true || final["has_more"] != false || final["metadata"].(map[string]any)["sha256"] == nil {
		t.Fatalf("closed artifact did not publish final truth: %#v", final)
	}
}

func TestRestartOrphanArtifactRemainsIncomplete(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("orphan")
	if err != nil {
		t.Fatal(err)
	}
	if err := writer.WriteRecord(1, []byte("partial-before-crash")); err != nil {
		t.Fatal(err)
	}
	restarted, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	page, err := restarted.readPage("orphan", "records", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	if page["eof"] != false || page["has_more"] != true || page["metadata"].(map[string]any)["sha256"] != nil || page["receipt"].(map[string]any)["reason"] != "unknown" {
		t.Fatalf("restart upgraded orphan to complete: %#v", page)
	}
	_ = writer.file.Close()
}

func TestRestartOrphanPTYRetainsLiveMetadataAndCombinedStream(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	writer, err := store.create("orphan-pty")
	if err != nil {
		t.Fatal(err)
	}
	writer.setMetadata("pty", "capture_order")
	if err := writer.WriteRecord(1, []byte("pty-output")); err != nil {
		t.Fatal(err)
	}
	terminalMetadata := filepath.Join(root, "logs", "orphan-pty.json")
	if err := store.writeCompanion("orphan-pty", terminalMetadata, []byte("{}\n")); err != nil {
		t.Fatal(err)
	}
	if err := writer.Abort(); err != nil {
		t.Fatal(err)
	}
	restarted, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	page, err := restarted.readPage("orphan-pty", "records", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	metadata := page["metadata"].(map[string]any)
	records := page["payload"].(map[string]any)["records"].([]map[string]any)
	if metadata["kind"] != "pty" || metadata["ordering"] != "capture_order" || len(records) != 1 || records[0]["stream"] != "combined" {
		t.Fatalf("restart lost orphan PTY semantics: %#v", page)
	}
	if page["receipt"].(map[string]any)["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("restart upgraded orphan PTY to complete: %#v", page)
	}
}

func TestLegacyCloseFailureAbortsCompletionMarker(t *testing.T) {
	root := t.TempDir()
	store, err := newArtifactStore(root, 4096, 2048, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	capture, err := newCommandCapture(root, "close-fault", 64, store)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := capture.stdout.Write([]byte("durable\n")); err != nil {
		t.Fatal(err)
	}
	if err := capture.stdout.file.Close(); err != nil {
		t.Fatal(err)
	}
	if err := capture.Close(); err == nil {
		t.Fatal("legacy close fault was ignored")
	}
	page, err := store.readPage("close-fault", "records", "", 64)
	if err != nil {
		t.Fatal(err)
	}
	if page["eof"] != false || page["metadata"].(map[string]any)["sha256"] != nil || page["receipt"].(map[string]any)["applied"].(map[string]any)["source_complete"] != false {
		t.Fatalf("legacy close fault published completion: %#v", page)
	}
}
