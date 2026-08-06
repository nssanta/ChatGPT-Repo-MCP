package tools

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestBatchEditFilesDispatchesCanonicalOpDiscriminator(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	result := engine.Execute(context.Background(), "batch_edit_files", map[string]any{
		"dry_run": false,
		"atomic":  true,
		"operations": []any{
			map[string]any{"op": "ensure_directory", "path": "nested"},
		},
	})
	if result["ok"] != true {
		t.Fatalf("canonical op discriminator was not dispatched: %#v", result)
	}
	if result["operations_total"] != 1 {
		t.Fatalf("operations_total = %#v, want 1", result["operations_total"])
	}
	if _, err := os.Stat(filepath.Join(root, "nested")); err != nil {
		t.Fatalf("ensure_directory operation did not create directory: %v", err)
	}
}

func TestBatchEditFilesRejectsMissingDiscriminator(t *testing.T) {
	engine, _ := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	result := engine.Execute(context.Background(), "batch_edit_files", map[string]any{
		"dry_run": true,
		"atomic":  true,
		"operations": []any{
			map[string]any{"path": "nested", "find": "old", "replace": "new"},
		},
	})
	if result["ok"] != false {
		t.Fatalf("missing operation discriminator must fail closed: %#v", result)
	}
	results, ok := result["results"].([]map[string]any)
	if !ok || len(results) != 1 || results[0]["error_kind"] != "unknown_operation" {
		t.Fatalf("unexpected missing discriminator result: %#v", result)
	}
}
