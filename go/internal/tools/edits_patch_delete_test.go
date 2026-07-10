package tools

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestApplyPatchEdgeCoverage(t *testing.T) {
	engine, root := newTestEngine(t)

	runGitTest(t, root, "init", "-b", "main")
	runGitTest(t, root, "config", "user.email", "test@example.com")
	runGitTest(t, root, "config", "user.name", "Tester")
	if err := os.WriteFile(filepath.Join(root, "readme.md"), []byte("one\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	runGitTest(t, root, "add", "readme.md")
	runGitTest(t, root, "commit", "-m", "init")

	engine.settings.MaxPatchBytes = 4
	oversized := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":    ".",
		"patch":   "longer",
		"dry_run": false,
	})
	if oversized["error_kind"] != "invalid_patch" {
		t.Fatalf("oversized patch should be rejected: %#v", oversized)
	}
	engine.settings.MaxPatchBytes = 100_000

	outside := `diff --git a/../outside.txt b/../outside.txt
index 0000000..1111111 100644
--- a/../outside.txt
+++ b/../outside.txt
@@ -1 +1 @@
-one
+two`
	outsidePatch := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":    ".",
		"patch":   outside,
		"dry_run": false,
	})
	if outsidePatch["error_kind"] != "patch_rejected" {
		t.Fatalf("outside path should be rejected: %#v", outsidePatch)
	}

	badPatch := `diff --git a/readme.md b/readme.md
index 0000000..1111111 100644
--- a/readme.md
+++ b/readme.md
@@ -1 +1 @@
-two
+three`
	invalid := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":    ".",
		"patch":   badPatch,
		"dry_run": false,
	})
	if invalid["error_kind"] != "patch_apply_error" {
		t.Fatalf("invalid context should return patch apply error: %#v", invalid)
	}

	delete := `diff --git a/readme.md b/readme.md
deleted file mode 100644
index 5626abf..0000000
--- a/readme.md
+++ /dev/null
@@ -1 +0,0 @@
-one
`
	applied := engine.Execute(context.Background(), "apply_patch", map[string]any{
		"repo":    ".",
		"patch":   delete,
		"dry_run": false,
	})
	if applied["ok"] != true || applied["applied"].(bool) != true {
		t.Fatalf("delete patch should apply: %#v", applied)
	}
	if _, err := os.Stat(filepath.Join(root, "readme.md")); !os.IsNotExist(err) {
		t.Fatalf("readme.md should be removed by patch: %v", err)
	}
}

func TestDeleteTextAndEnsureDirectoryAndBatchPartialCoverage(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("alpha\nbeta\ngamma\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	deletedByFind := engine.Execute(context.Background(), "delete_text_in_file", map[string]any{
		"path":    "notes.txt",
		"find":    "beta",
		"dry_run": false,
	})
	if deletedByFind["ok"] != true || deletedByFind["changed"] != true {
		t.Fatalf("find-based delete should remove text: %#v", deletedByFind)
	}
	if content, err := os.ReadFile(filepath.Join(root, "notes.txt")); err != nil || strings.TrimSpace(string(content)) != "alpha\n\ngamma" {
		t.Fatalf("unexpected content after find-delete: %q err=%v", content, err)
	}

	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("a\nb\nc\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	deletedByRange := engine.Execute(context.Background(), "delete_text_in_file", map[string]any{
		"path":       "notes.txt",
		"start_line": 2,
		"end_line":   3,
		"dry_run":    false,
	})
	if deletedByRange["ok"] != true || deletedByRange["changed"] != true {
		t.Fatalf("line-range delete should remove lines: %#v", deletedByRange)
	}
	if content, err := os.ReadFile(filepath.Join(root, "notes.txt")); err != nil || strings.TrimSpace(string(content)) != "a" {
		t.Fatalf("unexpected content after line delete: %q err=%v", content, err)
	}

	rejectedDirectory := engine.ensureDirectory("../outside", false)
	if rejectedDirectory["error_kind"] != "directory_rejected" {
		t.Fatalf("ensure_directory should reject path outside workspace: %#v", rejectedDirectory)
	}

	if err := os.WriteFile(filepath.Join(root, "batch-keep.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	nonAtomic := engine.batchEdits([]map[string]any{
		{"operation": "create", "path": "keep.txt", "content": "first"},
		{"operation": "replace", "path": "missing.txt", "find": "x", "replace": "y"},
	}, false, false, "non-atomic")
	if nonAtomic["ok"] != false {
		t.Fatalf("non-atomic batch should fail on invalid op: %#v", nonAtomic)
	}
	if nonAtomic["rolled_back"].(bool) {
		t.Fatalf("non-atomic batch should not rollback: %#v", nonAtomic)
	}
	if _, err := os.Stat(filepath.Join(root, "keep.txt")); err != nil {
		t.Fatalf("first operation should remain after non-atomic failure: %v", err)
	}
}
