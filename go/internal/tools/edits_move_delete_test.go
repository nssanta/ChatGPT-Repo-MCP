package tools

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"testing"
)

func TestMovePathHardBranches(t *testing.T) {
	engine, root := newTestEngine(t)

	disabled := engine.movePath("a.txt", "b.txt", false, nil, false)
	if disabled["error_kind"] != "operation_disabled" {
		t.Fatalf("disabled move should fail: %#v", disabled)
	}

	if err := os.WriteFile(filepath.Join(root, "source.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}

	engine.settings.AllowMoveDeleteOperations = true
	outside := engine.movePath(filepath.Join("..", "outside.txt"), "target.txt", false, nil, false)
	if outside["error_kind"] != "move_rejected" {
		t.Fatalf("outside source should be rejected: %#v", outside)
	}

	missing := engine.movePath("missing.txt", "target.txt", false, nil, false)
	if missing["error_kind"] != "move_failed" {
		t.Fatalf("missing source should fail on rename path: %#v", missing)
	}

	if err := os.WriteFile(filepath.Join(root, "dest.txt"), []byte("d"), 0o644); err != nil {
		t.Fatal(err)
	}
	enabled := engine.movePath("source.txt", "dest.txt", true, nil, false)
	if enabled["ok"] != true {
		t.Fatalf("overwrite move should replace destination: %#v", enabled)
	}
}

func TestDeletePathErrorBranches(t *testing.T) {
	engine, root := newTestEngine(t)

	disabled := engine.deletePath("x.txt", nil, false)
	if disabled["error_kind"] != "operation_disabled" {
		t.Fatalf("delete should be disabled by default: %#v", disabled)
	}
	engine.settings.AllowMoveDeleteOperations = true

	outside := engine.deletePath(filepath.Join("..", "outside.txt"), nil, false)
	if outside["error_kind"] != "delete_rejected" {
		t.Fatalf("outside deletion should be rejected: %#v", outside)
	}

	if err := os.WriteFile(filepath.Join(root, "guard.txt"), []byte("v"), 0o644); err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256([]byte("v"))
	wrong := hex.EncodeToString(sum[:]) + "x"
	stale := engine.deletePath("guard.txt", &wrong, false)
	if stale["error_kind"] != "stale_write" {
		t.Fatalf("wrong hash should fail delete: %#v", stale)
	}

	if err := os.Mkdir(filepath.Join(root, "locked"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "locked", "file.txt"), []byte("v"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(filepath.Join(root, "locked"), 0o555); err != nil {
		t.Fatal(err)
	}
	fail := engine.deletePath("locked/file.txt", nil, false)
	if fail["error_kind"] != "delete_failed" {
		t.Fatalf("permission issue should surface as delete_failed: %#v", fail)
	}
	if err := os.Chmod(filepath.Join(root, "locked"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.RemoveAll(filepath.Join(root, "locked")); err != nil {
		t.Fatal(err)
	}
}
