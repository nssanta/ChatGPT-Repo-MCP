package tools

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

func ageFile(t *testing.T, path string, age time.Duration) {
	t.Helper()
	stamp := time.Now().Add(-age)
	if err := os.Chtimes(path, stamp, stamp); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeMaintenancePrunesOnlyExpiredDurableFiles(t *testing.T) {
	root := t.TempDir()
	settings := testSettings(root)
	settings.ArtifactTTL = time.Hour
	settings.AuditLogTTL = time.Hour
	settings.MaintenanceEnabled = false

	jobs := settings.CommandJobsDir
	artifacts := filepath.Join(jobs, "artifacts")
	locks := filepath.Join(jobs, "locks")
	if err := os.MkdirAll(artifacts, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(locks, 0o700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Dir(settings.CommandAuditLogPath), 0o700); err != nil {
		t.Fatal(err)
	}

	files := map[string]string{
		"activeAudit":   settings.CommandAuditLogPath,
		"oldRotated":    settings.CommandAuditLogPath + ".1",
		"recentRotated": settings.CommandAuditLogPath + ".2",
		"oldLock":       filepath.Join(locks, "old.json"),
		"recentLock":    filepath.Join(locks, "recent.json"),
		"oldTmp":        filepath.Join(artifacts, "abandoned.tmp"),
		"recentTmp":     filepath.Join(artifacts, "fresh.tmp"),
	}
	for _, path := range files {
		if err := os.WriteFile(path, []byte("x"), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	for _, key := range []string{"oldRotated", "oldLock", "oldTmp"} {
		ageFile(t, files[key], 2*time.Hour)
	}

	engine := New(settings, nil)
	defer engine.Shutdown()
	result := engine.runMaintenanceOnce(time.Now())

	if result.AuditFilesRemoved != 1 {
		t.Fatalf("audit files removed = %d, want 1", result.AuditFilesRemoved)
	}
	if result.RuntimeFilesRemoved != 1 {
		t.Fatalf("runtime files removed = %d, want 1", result.RuntimeFilesRemoved)
	}
	for _, key := range []string{"activeAudit", "recentRotated", "oldLock", "recentLock", "recentTmp"} {
		if _, err := os.Stat(files[key]); err != nil {
			t.Fatalf("%s unexpectedly removed: %v", key, err)
		}
	}
	for _, key := range []string{"oldRotated", "oldTmp"} {
		if _, err := os.Stat(files[key]); !os.IsNotExist(err) {
			t.Fatalf("%s still exists, err=%v", key, err)
		}
	}
}
