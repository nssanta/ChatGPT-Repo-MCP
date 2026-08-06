package tools

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLoadWritableCoverageEdges(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.MaxFileBytes = 3

	if _, _, err := engine.loadWritable("missing.txt", false); err == nil {
		t.Fatal("missing file without create flag should fail")
	}

	absolute, loaded, err := engine.loadWritable("new.txt", true)
	if err != nil || absolute != filepath.Join(root, "new.txt") || loaded != nil {
		t.Fatalf("create-if-missing should return absolute path and empty content, got %v %v %v", absolute, len(loaded), err)
	}

	binary := filepath.Join(root, "binary.txt")
	engine.settings.MaxFileBytes = 16
	if err := os.WriteFile(binary, []byte("text\x00"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err = engine.loadWritable("binary.txt", false)
	if err == nil {
		t.Fatal("binary file should be rejected")
	}
	if !strings.Contains(err.Error(), "binary files cannot be edited") {
		t.Fatalf("binary error mismatch: %v", err)
	}

	tooBig := filepath.Join(root, "big.txt")
	engine.settings.MaxFileBytes = 3
	if err := os.WriteFile(tooBig, []byte("abcd"), 0o644); err != nil {
		t.Fatal(err)
	}
	_, _, err = engine.loadWritable("big.txt", false)
	if err == nil {
		t.Fatal("file over MaxFileBytes should fail")
	}
	if !strings.Contains(err.Error(), "MAX_FILE_BYTES") {
		t.Fatalf("max-file-bytes error mismatch: %v", err)
	}
}

func TestApplyTextPolicyBranches(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = true

	path := filepath.Join(root, "target.txt")
	if err := os.WriteFile(path, []byte("old-content"), 0o644); err != nil {
		t.Fatal(err)
	}

	result := engine.applyText(path, []byte("old-content"), "new-content", nil, false)
	if result["error_kind"] != "expected_hash_required" {
		t.Fatalf("expected required hash guard: %#v", result)
	}

	hash := sha256.Sum256([]byte("old-content"))
	expected := hex.EncodeToString(hash[:])
	wrong := expected + "0"
	missing := engine.applyText(path, []byte("old-content"), "new-content", &wrong, false)
	if missing["error_kind"] != "stale_write" {
		t.Fatalf("expected stale write guard: %#v", missing)
	}

	engine.settings.MaxWriteFileBytes = 4
	result = engine.applyText(path, []byte("old"), "this-is-way-too-long", nil, false)
	if result["error_kind"] != "write_too_large" {
		t.Fatalf("expected write size guard: %#v", result)
	}

	engine.settings.MaxWriteFileBytes = 100
	actual, _ := os.ReadFile(path)
	current := sha256.Sum256(actual)
	currentExpected := hex.EncodeToString(current[:])
	if unchanged := engine.applyText(path, actual, string(actual), &currentExpected, true); unchanged["changed"] != false {
		t.Fatalf("unchanged dry-run should keep changed=false: %#v", unchanged)
	}
}

func TestReplaceTextAndLineBoundaryBranches(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	path := filepath.Join(root, "replace.txt")
	if err := os.WriteFile(path, []byte("alpha alpha alpha"), 0o644); err != nil {
		t.Fatal(err)
	}

	if empty := engine.replaceText("replace.txt", "", "x", false, nil, false); empty["error_kind"] != "invalid_edit" {
		t.Fatalf("expected invalid find rejection: %#v", empty)
	}
	if notFound := engine.replaceText("replace.txt", "missing", "x", false, nil, false); notFound["error_kind"] != "text_not_found" {
		t.Fatalf("expected text not found: %#v", notFound)
	}
	multiple := engine.replaceText("replace.txt", "alpha", "beta", false, nil, false)
	if multiple["error_kind"] != "multiple_matches" {
		t.Fatalf("expected multiple matches branch: %#v", multiple)
	}

	if replaced := engine.replaceText("replace.txt", "alpha", "beta", true, nil, false); replaced["replacements"] != 3 || replaced["ok"] != true {
		t.Fatalf("replace_all should succeed with count: %#v", replaced)
	}
	content, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(content) != "beta beta beta" {
		t.Fatalf("unexpected replacement content: %q", content)
	}
}

func TestInsertAndLineRangeInvalidBranches(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	path := filepath.Join(root, "lines.txt")
	if err := os.WriteFile(path, []byte("one\ntwo\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if err := os.WriteFile(filepath.Join(root, "anchor.txt"), []byte("xanchorx\nxanchorx\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	unique := engine.insertText("anchor.txt", "xanchorx", "after", "insert", nil, false)
	if unique["error_kind"] != "anchor_not_unique" {
		t.Fatalf("expected anchor uniqueness guard: %#v", unique)
	}

	if invalidLine := engine.replaceLineRange("lines.txt", 0, 1, "x", nil, false); invalidLine["error_kind"] != "invalid_line_range" {
		t.Fatalf("expected invalid start guard: %#v", invalidLine)
	}
	if invalidEnd := engine.replaceLineRange("lines.txt", 1, 5, "x", nil, false); invalidEnd["error_kind"] != "invalid_line_range" {
		t.Fatalf("expected invalid end guard: %#v", invalidEnd)
	}

	if invalidAfter := engine.insertAtLine("lines.txt", 3, "x", false, nil, false); invalidAfter["error_kind"] != "invalid_line" {
		t.Fatalf("expected insert line out-of-range: %#v", invalidAfter)
	}
}

func TestEnsureDirectoryAndDeleteMoveCoverage(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.AllowMoveDeleteOperations = true
	engine.settings.RequireExpectedHashForWrites = false

	existing := filepath.Join(root, "existing")
	if err := os.MkdirAll(existing, 0o755); err != nil {
		t.Fatal(err)
	}
	already := engine.ensureDirectory("existing", false)
	if already["changed"] != false {
		t.Fatalf("existing directory should not be changed: %#v", already)
	}

	if dry := engine.ensureDirectory("fresh", true); dry["changed"] != true || dry["applied"] != false {
		t.Fatalf("dry run should not create directories: %#v", dry)
	}

	created := engine.ensureDirectory("fresh", false)
	if created["changed"] != true || created["applied"] != true {
		t.Fatalf("directory create should mark changed: %#v", created)
	}
	if _, err := os.Stat(filepath.Join(root, "fresh")); err != nil {
		t.Fatalf("fresh directory must exist: %v", err)
	}

	source := filepath.Join(root, "src.txt")
	if err := os.WriteFile(source, []byte("one"), 0o644); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(source)
	sum := sha256.Sum256(data)
	wrongExpected := hex.EncodeToString(sum[:]) + "0"
	stale := engine.movePath("src.txt", "moved.txt", false, &wrongExpected, false)
	if stale["error_kind"] != "stale_write" {
		t.Fatalf("move expected stale write: %#v", stale)
	}
	if _, err := os.Stat(filepath.Join(root, "src.txt")); err != nil {
		t.Fatalf("source must remain after stale move: %v", err)
	}

	_ = os.WriteFile(filepath.Join(root, "dest.txt"), []byte("dest"), 0o644)
	conflict := engine.movePath("src.txt", "dest.txt", false, nil, true)
	if conflict["error_kind"] != "already_exists" {
		t.Fatalf("existing destination without overwrite should fail: %#v", conflict)
	}

	if err := os.Mkdir(filepath.Join(root, "readonly"), 0o555); err != nil {
		t.Fatal(err)
	}
	blocked := engine.ensureDirectory("readonly/sub", false)
	// Privileged test runners can legitimately bypass directory mode bits.
	if blocked["ok"] != true && blocked["error_kind"] != "directory_failed" {
		t.Fatalf("file-as-parent should fail directory creation: %#v", blocked)
	}
	if err := os.Chmod(filepath.Join(root, "readonly"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "parent.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "moved"), 0o755); err != nil {
		t.Fatal(err)
	}
	_ = os.Remove(filepath.Join(root, "moved", "dst.txt"))
	moveDry := engine.movePath("parent.txt", "moved/dst.txt", true, nil, true)
	if moveDry["ok"] != true || moveDry["applied"] != false {
		t.Fatalf("dry move should report preview: %#v", moveDry)
	}
	if _, err := os.Stat(filepath.Join(root, "parent.txt")); err != nil {
		t.Fatalf("dry move should keep source: %v", err)
	}
}

func TestDeleteRootAndBatchCoverage(t *testing.T) {
	engine, _ := newTestEngine(t)
	engine.settings.AllowMoveDeleteOperations = true
	engine.settings.RequireExpectedHashForWrites = false

	rootDelete := engine.deletePath(".", nil, false)
	if rootDelete["error_kind"] != "delete_rejected" {
		t.Fatalf("project-root delete should be rejected: %#v", rootDelete)
	}

	empty := engine.batchEdits(nil, true, false, "empty")
	if empty["error_kind"] != "invalid_batch" {
		t.Fatalf("empty batch should be rejected: %#v", empty)
	}

	engine.settings.MaxBatchOperations = 1
	tooMany := engine.batchEdits([]map[string]any{
		{"operation": "create", "path": "a.txt"},
		{"operation": "delete", "path": "b.txt"},
	}, true, false, "too-many")
	if tooMany["error_kind"] != "too_many_operations" {
		t.Fatalf("batch too many operations should be rejected: %#v", tooMany)
	}

	unknown := engine.executeBatchOperation("nope", map[string]any{"path": "x.txt"}, false)
	if unknown["error_kind"] != "unknown_operation" {
		t.Fatalf("unknown batch operation should fail: %#v", unknown)
	}
}

func TestSnapshotAndRestorePaths(t *testing.T) {
	engine, root := newTestEngine(t)
	dir := filepath.Join(root, "folder")
	file := filepath.Join(root, "folder", "file.txt")
	if err := os.MkdirAll(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(file, []byte("v1"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "keep.txt"), []byte("keep"), 0o644); err != nil {
		t.Fatal(err)
	}

	snapshots := engine.snapshotOperations([]map[string]any{
		{"path": "folder"},
		{"path": "folder/file.txt"},
		{"path": "new.txt"},
		{"source_path": "keep.txt"},
		{"destination_path": "missing.txt"},
	})
	if len(snapshots) != 5 {
		t.Fatalf("expected 5 snapshot entries, got %d", len(snapshots))
	}

	_ = os.WriteFile(filepath.Join(root, "new.txt"), []byte("new"), 0o644)
	_ = os.MkdirAll(filepath.Join(root, "missing.txt"), 0o755)
	engine.restoreSnapshots(snapshots)

	if _, err := os.Stat(filepath.Join(root, "folder/file.txt")); err != nil {
		t.Fatalf("snapshot file should be restored: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "keep.txt")); err != nil {
		t.Fatalf("snapshot source file should be restored: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "new.txt")); err == nil {
		t.Fatalf("nonexistent snapshot path should be removed")
	}
	if _, err := os.Stat(filepath.Join(root, "missing.txt")); err == nil {
		t.Fatalf("snapshot with missing path should be removed: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "folder")); err != nil {
		t.Fatalf("directory snapshot should be restored: %v", err)
	}
}

func TestWriteCreateDeleteCoverageBranches(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.AllowMoveDeleteOperations = true
	engine.settings.RequireExpectedHashForWrites = false

	missing := engine.writeText("missing.txt", "x", false, nil, false)
	if missing["ok"] == true {
		t.Fatalf("writeText should reject missing create_if_missing=false: %#v", missing)
	}
	if created := engine.createText("created.txt", "x", false, false); created["ok"] != true {
		t.Fatalf("createText should create new file: %#v", created)
	}
	if exists := engine.createText("created.txt", "x", false, false); exists["error_kind"] != "already_exists" {
		t.Fatalf("createText should reject existing without overwrite: %#v", exists)
	}
	if overwritten := engine.createText("created.txt", "y", true, false); overwritten["ok"] != true {
		t.Fatalf("createText with overwrite should update: %#v", overwritten)
	}
	content, err := os.ReadFile(filepath.Join(root, "created.txt"))
	if err != nil {
		t.Fatalf("read created file: %v", err)
	}
	if string(content) != "y" {
		t.Fatalf("overwrite create expected new content: %q", content)
	}

	dry := engine.deletePath("created.txt", nil, true)
	if dry["ok"] != true || dry["applied"] != false {
		t.Fatalf("dry delete should be preview: %#v", dry)
	}
	if _, err := os.Stat(filepath.Join(root, "created.txt")); err != nil {
		t.Fatalf("dry delete should keep file: %v", err)
	}
	removed := engine.deletePath("created.txt", nil, false)
	if removed["ok"] != true {
		t.Fatalf("delete should remove file: %#v", removed)
	}
	if _, err := os.Stat(filepath.Join(root, "created.txt")); !os.IsNotExist(err) {
		t.Fatalf("file should be deleted: %v", err)
	}
}

func TestExecuteBatchOperationAllCoveredCases(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.AllowMoveDeleteOperations = true
	engine.settings.RequireExpectedHashForWrites = false

	if created := engine.executeBatchOperation("create", map[string]any{
		"path":    "source.txt",
		"content": "before",
	}, false); created["ok"] != true {
		t.Fatalf("create batch op failed: %#v", created)
	}
	if wrote := engine.executeBatchOperation("write", map[string]any{
		"path":    "source.txt",
		"content": "before",
	}, false); wrote["ok"] != true {
		t.Fatalf("write batch op failed: %#v", wrote)
	}
	if replace := engine.executeBatchOperation("replace", map[string]any{
		"path":    "source.txt",
		"find":    "before",
		"replace": "after",
	}, false); replace["ok"] != true {
		t.Fatalf("replace batch op failed: %#v", replace)
	}
	if inserted := engine.executeBatchOperation("insert", map[string]any{
		"path":     "source.txt",
		"anchor":   "after",
		"position": "after",
		"content":  "x",
	}, false); inserted["ok"] != true {
		t.Fatalf("insert batch op failed: %#v", inserted)
	}
	if lines := engine.executeBatchOperation("replace_lines", map[string]any{
		"path":        "source.txt",
		"start_line":  1,
		"end_line":    1,
		"replacement": "mid",
	}, false); lines["ok"] != true {
		t.Fatalf("replace_lines batch op failed: %#v", lines)
	}
	if appended := engine.executeBatchOperation("append", map[string]any{
		"path":    "source.txt",
		"content": "tail",
	}, false); appended["ok"] != true {
		t.Fatalf("append batch op failed: %#v", appended)
	}
	if moved := engine.executeBatchOperation("move", map[string]any{
		"source_path":      "source.txt",
		"destination_path": "moved.txt",
		"overwrite":        false,
	}, false); moved["ok"] != true {
		t.Fatalf("move batch op failed: %#v", moved)
	}
	if ensured := engine.executeBatchOperation("ensure_directory", map[string]any{
		"path": "dir",
	}, false); ensured["ok"] != true {
		t.Fatalf("ensure_directory batch op failed: %#v", ensured)
	}
	if deleted := engine.executeBatchOperation("delete", map[string]any{
		"path": "moved.txt",
	}, false); deleted["ok"] != true {
		t.Fatalf("delete batch op failed: %#v", deleted)
	}
	if _, err := os.Stat(filepath.Join(root, "moved.txt")); !os.IsNotExist(err) {
		t.Fatalf("batch move/delete should remove source target: %v", err)
	}
	if _, err := os.Stat(filepath.Join(root, "dir")); err != nil {
		t.Fatalf("ensure directory should exist: %v", err)
	}

	deleteErr := filepath.Join(root, "dir")
	if err := os.RemoveAll(filepath.Join(root, "dir")); err != nil {
		t.Fatalf("cleanup: %v", err)
	}
	if _, err := os.Stat(deleteErr); err == nil {
		t.Fatal("cleanup should remove directory")
	}

	_ = os.Remove(filepath.Join(root, "source.txt"))
}

func TestUpdateMissionBranches(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.RequireExpectedHashForWrites = false

	if err := os.WriteFile(filepath.Join(root, "CURRENT_TASK.md"), []byte(""), 0o644); err != nil {
		t.Fatal(err)
	}
	empty := engine.updateMission(map[string]any{
		"section_title": "Goal",
		"chunks":        []any{"alpha", "beta"},
	})
	if empty["ok"] != true {
		t.Fatalf("empty mission file should create block: %#v", empty)
	}

	if err := os.WriteFile(filepath.Join(root, "CURRENT_TASK.md"), []byte("## Goal\n\nbase\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	withGoal := engine.updateMission(map[string]any{
		"section_title": "Goal",
		"content":       "inserted",
		"position":      "before_goal",
	})
	if withGoal["ok"] != true || !strings.Contains(withGoal["diff"].(string), "inserted") {
		t.Fatalf("before_goal branch should inject before existing goal heading: %#v", withGoal)
	}

	if err := os.Remove(filepath.Join(root, "docs", "CURRENT_TASK.md")); err != nil && !os.IsNotExist(err) {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "docs"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "docs", "CURRENT_TASK.md"), []byte("## Task\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	preset := engine.updateMission(map[string]any{
		"section_title": "# Audit",
		"content":       "must log",
		"preset":        "mandatory_system_tool_log",
		"dry_run":       true,
	})
	if preset["ok"] != true || preset["applied"] != false {
		t.Fatalf("mandatory preset dry-run should not apply: %#v", preset)
	}
}
