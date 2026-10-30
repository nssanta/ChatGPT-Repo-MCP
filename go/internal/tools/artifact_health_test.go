package tools

import (
	"context"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestArtifactStoreUsesConfiguredDiskReserve(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 1024, 1024, time.Hour, 100)
	if err != nil {
		t.Fatal(err)
	}
	store.diskChecked = time.Now().Add(time.Hour)
	store.diskAvailable = 200
	store.diskCapacity = 10000
	if err := store.reserveWrite("test", 100, 100); err != nil {
		t.Fatalf("write preserving configured reserve rejected: %v", err)
	}
}

func TestArtifactStoreRejectsWriteBelowDiskReserve(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 1024, 1024, time.Hour, 100)
	if err != nil {
		t.Fatal(err)
	}
	store.diskChecked = time.Now().Add(time.Hour)
	store.diskAvailable = 199
	store.diskCapacity = 10000
	if err := store.reserveWrite("test", 100, 100); err == nil || !strings.Contains(err.Error(), "disk reserve 100 bytes") {
		t.Fatalf("expected disk reserve rejection, got %v", err)
	}
}

func TestDoctorReportsUnavailableArtifactStorage(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.ArtifactDiskReserveBytes = math.MaxInt64
	result := New(settings, nil).doctor(context.Background())
	storage, ok := result["artifact_storage"].(map[string]any)
	if !ok || storage["ok"] != false || result["ok"] != false {
		t.Fatalf("doctor missed unavailable storage: %#v", result)
	}
	if storage["reserve_bytes"] != int64(math.MaxInt64) || !strings.Contains(storage["error"].(string), "disk reserve") {
		t.Fatalf("missing reserve diagnosis: %#v", storage)
	}
}

func TestDoctorReportsArtifactStorageInitializationFailure(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.CommandJobsDir = filepath.Join(t.TempDir(), "file")
	if err := os.WriteFile(settings.CommandJobsDir, []byte("occupied"), 0o600); err != nil {
		t.Fatal(err)
	}
	result := New(settings, nil).doctor(context.Background())
	storage, ok := result["artifact_storage"].(map[string]any)
	if !ok || storage["ok"] != false || result["ok"] != false {
		t.Fatalf("doctor missed initialization failure: %#v", result)
	}
}

func TestDoctorChecksWritableArtifactStorage(t *testing.T) {
	settings := testSettings(t.TempDir())
	settings.ArtifactDiskReserveBytes = 0
	engine := New(settings, nil)
	result := engine.doctor(context.Background())
	storage, ok := result["artifact_storage"].(map[string]any)
	if !ok || storage["ok"] != true || result["ok"] != true {
		t.Fatalf("doctor rejected writable storage: %#v", result)
	}
	entries, err := os.ReadDir(filepath.Join(settings.CommandJobsDir, "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), ".health-") {
			t.Fatalf("health probe was not removed: %s", entry.Name())
		}
	}
}

func TestDoctorStorageCheckHonorsCancellation(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	result := New(testSettings(t.TempDir()), nil).doctor(ctx)
	if result["ok"] != false {
		t.Fatalf("doctor ignored cancellation: %#v", result)
	}
	if storage, ok := result["artifact_storage"].(map[string]any); ok {
		if storage["ok"] != false {
			t.Fatalf("storage check ignored cancellation: %#v", storage)
		}
	} else if result["error_kind"] != "operation_cancelled" {
		t.Fatalf("doctor omitted cancellation diagnosis: %#v", result)
	}
}

func TestDoctorDetectsArtifactProbeFailure(t *testing.T) {
	engine := New(testSettings(t.TempDir()), nil)
	store, err := engine.artifactStore()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Rename(store.root, store.root+"-saved"); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(store.root, []byte("occupied"), 0o600); err != nil {
		t.Fatal(err)
	}
	result := engine.doctor(context.Background())
	storage, ok := result["artifact_storage"].(map[string]any)
	if !ok || storage["ok"] != false || result["ok"] != false {
		t.Fatalf("doctor missed probe failure: %#v", result)
	}
	if !strings.Contains(storage["error"].(string), "create artifact storage probe") {
		t.Fatalf("missing probe diagnosis: %#v", storage)
	}
}

func TestArtifactStoreAllowsZeroDiskReserve(t *testing.T) {
	store, err := newArtifactStore(t.TempDir(), 1024, 1024, time.Hour, 0)
	if err != nil {
		t.Fatal(err)
	}
	store.diskChecked = time.Now().Add(time.Hour)
	store.diskAvailable = 1
	store.diskCapacity = 10000
	if err := store.reserveWrite("test", 1, 1); err != nil {
		t.Fatalf("zero reserve rejected available space: %v", err)
	}
}
