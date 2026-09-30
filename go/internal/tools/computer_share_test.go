package tools

import (
	"context"
	"encoding/base64"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestMaterializeComputerShareUsesManagedFileResource(t *testing.T) {
	cache := t.TempDir()
	t.Setenv("XDG_CACHE_HOME", cache)
	root := t.TempDir()
	settings := testSettings(root)
	settings.MaintenanceEnabled = false
	settings.FileTransferExportMaxBytes = 4096
	engine := New(settings, nil)
	defer engine.Shutdown()

	payload := []byte("\x89PNG\r\n\x1a\ncomputer-snapshot")
	result := engine.materializeComputerShare(map[string]any{
		"ok":          true,
		"snapshot_id": "abcdef1234567890",
		"mime_type":   "image/png",
		"image_b64":   base64.StdEncoding.EncodeToString(payload),
	})
	if result["ok"] != true {
		t.Fatalf("share result = %#v", result)
	}
	path, _ := result["path"].(string)
	wantDir := filepath.Join(cache, "chatrepo-mcp", "shared-screens")
	if filepath.Dir(path) != wantDir {
		t.Fatalf("share dir = %q, want %q", filepath.Dir(path), wantDir)
	}
	if info, err := os.Stat(path); err != nil || !info.Mode().IsRegular() {
		t.Fatalf("materialized file stat = %#v err=%v", info, err)
	}
	uri, _ := result["resource_uri"].(string)
	if !strings.HasPrefix(uri, exportedFileResourcePrefix) {
		t.Fatalf("resource uri = %q", uri)
	}
	data, mimeType, err := engine.ReadExportResource(uri)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(payload) || mimeType != "image/png" {
		t.Fatalf("resource = %q %q", data, mimeType)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatal(err)
	}
	removed := cleanupComputerShareDirectory(wantDir, info.ModTime().Add(computerShareTTL+time.Second))
	if removed < 1 {
		t.Fatalf("removed = %d, want >= 1", removed)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Fatalf("shared file still exists: %v", err)
	}

	_ = context.Background() // keep this test package's imports stable across platform builds
}
