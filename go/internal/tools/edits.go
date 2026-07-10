package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
)

type editSnapshot struct {
	path   string
	exists bool
	data   []byte
	mode   os.FileMode
	isDir  bool
}

func (e *Engine) executeEditTool(ctx context.Context, name string, args map[string]any) map[string]any {
	switch name {
	case "write_text_file":
		return e.writeText(stringArg(args, "path", ""), stringArg(args, "content", ""), boolArg(args, "create_if_missing", false), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "create_text_file":
		return e.createText(stringArg(args, "path", ""), stringArg(args, "content", ""), boolArg(args, "overwrite", false), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "replace_text_in_file":
		return e.replaceText(stringArg(args, "path", ""), stringArg(args, "find", ""), stringArg(args, "replace", ""), boolArg(args, "replace_all", false), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "insert_text_in_file":
		return e.insertText(stringArg(args, "path", ""), stringArg(args, "anchor", ""), stringArg(args, "position", "after"), stringArg(args, "content", ""), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "delete_text_in_file":
		return e.deleteText(args)
	case "replace_lines":
		return e.replaceLineRange(stringArg(args, "path", ""), intArg(args, "start_line", 0), intArg(args, "end_line", 0), stringArg(args, "replacement", ""), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "insert_before_line", "insert_after_line":
		return e.insertAtLine(stringArg(args, "path", ""), intArg(args, "line", 0), stringArg(args, "content", ""), name == "insert_after_line", optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "insert_before_heading", "insert_after_heading":
		return e.insertAtHeading(stringArg(args, "path", ""), stringArg(args, "heading", ""), stringArg(args, "content", ""), name == "insert_after_heading", optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "append_to_file":
		return e.appendText(stringArg(args, "path", ""), stringArg(args, "content", ""), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "ensure_directory":
		return e.ensureDirectory(stringArg(args, "path", ""), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "move_path":
		return e.movePath(stringArg(args, "source_path", ""), stringArg(args, "destination_path", ""), boolArg(args, "overwrite", false), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "delete_path":
		return e.deletePath(stringArg(args, "path", ""), optionalString(args, "expected_sha256"), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")))
	case "batch_edit_files", "apply_change_set":
		return e.batchEdits(mapsArg(args, "operations"), boolArg(args, "atomic", true), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")), stringArg(args, "name", ""))
	case "apply_patch":
		return e.applyPatch(ctx, stringArg(args, "patch", ""), e.settings.EffectiveDryRun(optionalBool(args, "dry_run")), optionalString(args, "expected_base_sha"), stringArg(args, "repo", ""))
	case "update_current_mission":
		return e.updateMission(args)
	default:
		return failure("unknown_edit_tool", name)
	}
}

func (e *Engine) loadWritable(path string, createIfMissing bool) (string, []byte, error) {
	resolved, err := e.perimeter.Resolve(path, true, true)
	if err != nil {
		return "", nil, err
	}
	data, err := os.ReadFile(resolved.Absolute)
	if err != nil {
		if os.IsNotExist(err) && createIfMissing {
			return resolved.Absolute, nil, nil
		}
		return "", nil, err
	}
	if int64(len(data)) > e.settings.MaxFileBytes {
		return "", nil, fmt.Errorf("file exceeds MAX_FILE_BYTES")
	}
	if strings.IndexByte(string(data), 0) >= 0 {
		return "", nil, fmt.Errorf("binary files cannot be edited")
	}
	return resolved.Absolute, data, nil
}

func (e *Engine) applyText(path string, oldData []byte, newText string, expected *string, dryRun bool) map[string]any {
	if int64(len(newText)) > e.settings.MaxWriteFileBytes {
		return failure("write_too_large", fmt.Sprintf("content exceeds MAX_WRITE_FILE_BYTES (%d > %d)", len(newText), e.settings.MaxWriteFileBytes))
	}
	oldDigest := sha256.Sum256(oldData)
	oldHash := hex.EncodeToString(oldDigest[:])
	if expected != nil && *expected != oldHash {
		return map[string]any{"ok": false, "error_kind": "stale_write", "error": "expected_sha256 does not match current file", "path": e.perimeter.Display(path), "expected_sha256": *expected, "actual_sha256": oldHash}
	}
	if expected == nil && fileExists(path) && e.settings.RequireExpectedHashForWrites {
		return map[string]any{"ok": false, "error_kind": "expected_hash_required", "error": "expected_sha256 is required for existing files", "path": e.perimeter.Display(path), "actual_sha256": oldHash}
	}
	newData := []byte(newText)
	newDigest := sha256.Sum256(newData)
	newHash := hex.EncodeToString(newDigest[:])
	changed := string(oldData) != newText
	result := map[string]any{
		"ok": true, "path": e.perimeter.Display(path), "dry_run": dryRun, "applied": changed && !dryRun,
		"changed": changed, "old_sha256": oldHash, "new_sha256": newHash,
		"diff": unifiedDiff(e.perimeter.Display(path), string(oldData), newText, e.settings.MaxCombinedDiffChars),
	}
	if !changed || dryRun {
		return result
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return withError("write_failed", err)
	}
	mode := os.FileMode(0o644)
	if info, err := os.Stat(path); err == nil {
		mode = info.Mode().Perm()
	}
	temporary, err := os.CreateTemp(filepath.Dir(path), ".chatrepo-write-*")
	if err != nil {
		return withError("write_failed", err)
	}
	temporaryPath := temporary.Name()
	defer os.Remove(temporaryPath)
	if _, err = temporary.Write(newData); err == nil {
		err = temporary.Chmod(mode)
	}
	if closeErr := temporary.Close(); err == nil {
		err = closeErr
	}
	if err == nil {
		err = os.Rename(temporaryPath, path)
	}
	if err != nil {
		return withError("write_failed", err)
	}
	return result
}

func (e *Engine) writeText(path, content string, createIfMissing bool, expected *string, dryRun bool) map[string]any {
	absolute, oldData, err := e.loadWritable(path, createIfMissing)
	if err != nil {
		return withError("write_rejected", err)
	}
	return e.applyText(absolute, oldData, content, expected, dryRun)
}

func (e *Engine) createText(path, content string, overwrite, dryRun bool) map[string]any {
	absolute, oldData, err := e.loadWritable(path, true)
	if err != nil {
		return withError("create_rejected", err)
	}
	if fileExists(absolute) && !overwrite {
		return failure("already_exists", fmt.Sprintf("file already exists: %s", path))
	}
	return e.applyText(absolute, oldData, content, nil, dryRun)
}

func (e *Engine) replaceText(path, find, replacement string, replaceAll bool, expected *string, dryRun bool) map[string]any {
	if find == "" {
		return failure("invalid_edit", "find must not be empty")
	}
	absolute, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	old := string(oldData)
	count := strings.Count(old, find)
	if count == 0 {
		return failure("text_not_found", "find text was not found")
	}
	if count > 1 && !replaceAll {
		return failure("multiple_matches", fmt.Sprintf("find text occurs %d times; set replace_all=true", count))
	}
	newText := strings.Replace(old, find, replacement, 1)
	if replaceAll {
		newText = strings.ReplaceAll(old, find, replacement)
	}
	result := e.applyText(absolute, oldData, newText, expected, dryRun)
	result["replacements"] = count
	if !replaceAll {
		result["replacements"] = 1
	}
	return result
}

func (e *Engine) insertText(path, anchor, position, content string, expected *string, dryRun bool) map[string]any {
	if position != "before" && position != "after" {
		return failure("invalid_position", "position must be before or after")
	}
	absolute, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	old := string(oldData)
	if strings.Count(old, anchor) != 1 {
		return failure("anchor_not_unique", "anchor must occur exactly once")
	}
	replacement := content + anchor
	if position == "after" {
		replacement = anchor + content
	}
	return e.applyText(absolute, oldData, strings.Replace(old, anchor, replacement, 1), expected, dryRun)
}

func (e *Engine) deleteText(args map[string]any) map[string]any {
	path := stringArg(args, "path", "")
	expected := optionalString(args, "expected_sha256")
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if find := optionalString(args, "find"); find != nil {
		return e.replaceText(path, *find, "", false, expected, dryRun)
	}
	start := intArg(args, "start_line", 0)
	end := intArg(args, "end_line", start)
	return e.replaceLineRange(path, start, end, "", expected, dryRun)
}

func (e *Engine) replaceLineRange(path string, start, end int, replacement string, expected *string, dryRun bool) map[string]any {
	if start < 1 || end < start {
		return failure("invalid_line_range", "line range must be 1-based and end_line >= start_line")
	}
	absolute, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	old := string(oldData)
	trailingNewline := strings.HasSuffix(old, "\n")
	lines := strings.Split(strings.TrimSuffix(old, "\n"), "\n")
	if end > len(lines) {
		return failure("invalid_line_range", fmt.Sprintf("end_line %d exceeds file length %d", end, len(lines)))
	}
	newLines := append([]string{}, lines[:start-1]...)
	if replacement != "" {
		newLines = append(newLines, strings.Split(strings.TrimSuffix(replacement, "\n"), "\n")...)
	}
	newLines = append(newLines, lines[end:]...)
	newText := strings.Join(newLines, "\n")
	if trailingNewline || strings.HasSuffix(replacement, "\n") {
		newText += "\n"
	}
	return e.applyText(absolute, oldData, newText, expected, dryRun)
}

func (e *Engine) insertAtLine(path string, line int, content string, after bool, expected *string, dryRun bool) map[string]any {
	if line < 1 {
		return failure("invalid_line", "line must be 1-based")
	}
	absolute, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	old := string(oldData)
	lines := strings.Split(strings.TrimSuffix(old, "\n"), "\n")
	if line > len(lines) {
		return failure("invalid_line", fmt.Sprintf("line %d exceeds file length %d", line, len(lines)))
	}
	index := line - 1
	if after {
		index++
	}
	insert := strings.Split(strings.TrimSuffix(content, "\n"), "\n")
	newLines := append([]string{}, lines[:index]...)
	newLines = append(newLines, insert...)
	newLines = append(newLines, lines[index:]...)
	newText := strings.Join(newLines, "\n")
	if strings.HasSuffix(old, "\n") {
		newText += "\n"
	}
	return e.applyText(absolute, oldData, newText, expected, dryRun)
}

func (e *Engine) insertAtHeading(path, heading, content string, after bool, expected *string, dryRun bool) map[string]any {
	_, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	lines := strings.Split(strings.TrimSuffix(string(oldData), "\n"), "\n")
	line := 0
	for index, value := range lines {
		if strings.TrimSpace(value) == strings.TrimSpace(heading) {
			if line != 0 {
				return failure("heading_not_unique", "heading must occur exactly once")
			}
			line = index + 1
		}
	}
	if line == 0 {
		return failure("heading_not_found", fmt.Sprintf("heading not found: %s", heading))
	}
	return e.insertAtLine(path, line, content, after, expected, dryRun)
}

func (e *Engine) appendText(path, content string, expected *string, dryRun bool) map[string]any {
	absolute, oldData, err := e.loadWritable(path, false)
	if err != nil {
		return withError("edit_rejected", err)
	}
	return e.applyText(absolute, oldData, string(oldData)+content, expected, dryRun)
}

func (e *Engine) ensureDirectory(path string, dryRun bool) map[string]any {
	resolved, err := e.perimeter.Resolve(path, true, true)
	if err != nil {
		return withError("directory_rejected", err)
	}
	_, statErr := os.Stat(resolved.Absolute)
	exists := statErr == nil
	if !dryRun && !exists {
		if err := os.MkdirAll(resolved.Absolute, 0o755); err != nil {
			return withError("directory_failed", err)
		}
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(resolved.Absolute), "dry_run": dryRun, "applied": !dryRun && !exists, "changed": !exists}
}

func (e *Engine) movePath(source, destination string, overwrite bool, expected *string, dryRun bool) map[string]any {
	if !e.settings.AllowMoveDeleteOperations {
		return failure("operation_disabled", "move/delete operations require ALLOW_MOVE_DELETE_OPERATIONS=true or ACCESS_MODE=full")
	}
	sourceResolved, err := e.perimeter.Resolve(source, true, true)
	if err != nil {
		return withError("move_rejected", err)
	}
	destinationResolved, err := e.perimeter.Resolve(destination, true, true)
	if err != nil {
		return withError("move_rejected", err)
	}
	if expected != nil {
		data, readErr := os.ReadFile(sourceResolved.Absolute)
		if readErr == nil {
			digest := sha256.Sum256(data)
			if hex.EncodeToString(digest[:]) != *expected {
				return failure("stale_write", "expected_sha256 does not match source")
			}
		}
	}
	if _, statErr := os.Lstat(destinationResolved.Absolute); statErr == nil && !overwrite {
		return failure("already_exists", "destination already exists")
	}
	if !dryRun {
		if overwrite {
			_ = os.RemoveAll(destinationResolved.Absolute)
		}
		if err := os.MkdirAll(filepath.Dir(destinationResolved.Absolute), 0o755); err != nil {
			return withError("move_failed", err)
		}
		if err := os.Rename(sourceResolved.Absolute, destinationResolved.Absolute); err != nil {
			return withError("move_failed", err)
		}
	}
	return map[string]any{"ok": true, "source_path": e.perimeter.Display(sourceResolved.Absolute), "destination_path": e.perimeter.Display(destinationResolved.Absolute), "dry_run": dryRun, "applied": !dryRun, "changed": true}
}

func (e *Engine) deletePath(path string, expected *string, dryRun bool) map[string]any {
	if !e.settings.AllowMoveDeleteOperations {
		return failure("operation_disabled", "move/delete operations require ALLOW_MOVE_DELETE_OPERATIONS=true or ACCESS_MODE=full")
	}
	resolved, err := e.perimeter.Resolve(path, true, true)
	if err != nil {
		return withError("delete_rejected", err)
	}
	if filepath.Clean(resolved.Absolute) == filepath.Clean(e.settings.ProjectRoot) {
		return failure("delete_rejected", "cannot delete PROJECT_ROOT")
	}
	if expected != nil {
		data, readErr := os.ReadFile(resolved.Absolute)
		if readErr == nil {
			digest := sha256.Sum256(data)
			if hex.EncodeToString(digest[:]) != *expected {
				return failure("stale_write", "expected_sha256 does not match current file")
			}
		}
	}
	if !dryRun {
		if err := os.RemoveAll(resolved.Absolute); err != nil {
			return withError("delete_failed", err)
		}
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(resolved.Absolute), "dry_run": dryRun, "applied": !dryRun, "changed": true}
}

func (e *Engine) batchEdits(operations []map[string]any, atomic, dryRun bool, name string) map[string]any {
	if len(operations) == 0 {
		return failure("invalid_batch", "operations must not be empty")
	}
	if len(operations) > e.settings.MaxBatchOperations {
		return failure("too_many_operations", fmt.Sprintf("operations exceed MAX_BATCH_OPERATIONS (%d > %d)", len(operations), e.settings.MaxBatchOperations))
	}
	snapshots := e.snapshotOperations(operations)
	results := make([]map[string]any, 0, len(operations))
	ok := true
	for _, operation := range operations {
		typeName := firstNonEmpty(stringArg(operation, "operation", ""), stringArg(operation, "type", ""))
		if typeName == "" {
			typeName = "replace_text"
		}
		result := e.executeBatchOperation(typeName, operation, dryRun)
		results = append(results, result)
		if result["ok"] == false {
			ok = false
			if atomic {
				if !dryRun {
					e.restoreSnapshots(snapshots)
				}
				break
			}
		}
	}
	combined := ""
	for _, result := range results {
		if diff, exists := result["diff"].(string); exists && diff != "" {
			combined += diff + "\n"
		}
	}
	combined, truncated := capText(combined, e.settings.MaxCombinedDiffChars)
	return map[string]any{"ok": ok, "name": name, "atomic": atomic, "dry_run": dryRun, "rolled_back": atomic && !ok && !dryRun, "results": results, "combined_diff": combined, "diff_truncated": truncated}
}

func (e *Engine) executeBatchOperation(name string, args map[string]any, dryRun bool) map[string]any {
	args = cloneMap(args)
	args["dry_run"] = dryRun
	switch name {
	case "write", "write_text", "write_text_file":
		return e.executeEditTool(context.Background(), "write_text_file", args)
	case "create", "create_text", "create_text_file":
		return e.executeEditTool(context.Background(), "create_text_file", args)
	case "replace", "replace_text", "replace_text_in_file":
		return e.executeEditTool(context.Background(), "replace_text_in_file", args)
	case "insert", "insert_text", "insert_text_in_file":
		return e.executeEditTool(context.Background(), "insert_text_in_file", args)
	case "delete_text", "delete_text_in_file":
		return e.executeEditTool(context.Background(), "delete_text_in_file", args)
	case "replace_lines":
		return e.executeEditTool(context.Background(), "replace_lines", args)
	case "append", "append_to_file":
		return e.executeEditTool(context.Background(), "append_to_file", args)
	case "move", "move_path":
		return e.executeEditTool(context.Background(), "move_path", args)
	case "delete", "delete_file", "delete_path":
		return e.executeEditTool(context.Background(), "delete_path", args)
	case "ensure_directory":
		return e.executeEditTool(context.Background(), "ensure_directory", args)
	default:
		return failure("unknown_operation", fmt.Sprintf("unknown batch operation: %s", name))
	}
}

func cloneMap(source map[string]any) map[string]any {
	result := make(map[string]any, len(source)+1)
	for key, value := range source {
		result[key] = value
	}
	return result
}

func (e *Engine) snapshotOperations(operations []map[string]any) []editSnapshot {
	paths := make(map[string]bool)
	for _, operation := range operations {
		for _, key := range []string{"path", "source_path", "destination_path"} {
			if path := stringArg(operation, key, ""); path != "" {
				if resolved, err := e.perimeter.Resolve(path, true, true); err == nil {
					paths[resolved.Absolute] = true
				}
			}
		}
	}
	ordered := make([]string, 0, len(paths))
	for path := range paths {
		ordered = append(ordered, path)
	}
	sort.Strings(ordered)
	result := make([]editSnapshot, 0, len(ordered))
	for _, path := range ordered {
		info, err := os.Lstat(path)
		if err != nil {
			result = append(result, editSnapshot{path: path})
			continue
		}
		snapshot := editSnapshot{path: path, exists: true, mode: info.Mode(), isDir: info.IsDir()}
		if !info.IsDir() {
			snapshot.data, _ = os.ReadFile(path)
		}
		result = append(result, snapshot)
	}
	return result
}

func (e *Engine) restoreSnapshots(snapshots []editSnapshot) {
	for _, snapshot := range snapshots {
		if !snapshot.exists {
			_ = os.RemoveAll(snapshot.path)
			continue
		}
		if snapshot.isDir {
			_ = os.MkdirAll(snapshot.path, snapshot.mode.Perm())
			continue
		}
		_ = os.MkdirAll(filepath.Dir(snapshot.path), 0o755)
		_ = os.WriteFile(snapshot.path, snapshot.data, snapshot.mode.Perm())
	}
}

var patchPathPattern = regexp.MustCompile(`(?m)^(?:---|\+\+\+)\s+(?:[ab]/)?([^\t\n]+)`)

func (e *Engine) applyPatch(ctx context.Context, patch string, dryRun bool, expectedBase *string, repo string) map[string]any {
	if patch == "" || len(patch) > e.settings.MaxPatchBytes {
		return failure("invalid_patch", "patch is empty or exceeds MAX_PATCH_BYTES")
	}
	toplevel, err := e.resolveRepo(ctx, repo)
	if err != nil {
		return withError("patch_rejected", err)
	}
	if expectedBase != nil {
		output, runErr := exec.CommandContext(ctx, "git", "-C", toplevel, "rev-parse", "HEAD").Output()
		if runErr != nil || strings.TrimSpace(string(output)) != *expectedBase {
			return failure("stale_base", "expected_base_sha does not match HEAD")
		}
	}
	for _, match := range patchPathPattern.FindAllStringSubmatch(patch, -1) {
		if len(match) < 2 || match[1] == "/dev/null" {
			continue
		}
		if _, resolveErr := e.perimeter.Resolve(filepath.Join(toplevel, match[1]), true, true); resolveErr != nil {
			return withError("patch_rejected", resolveErr)
		}
	}
	arguments := []string{"-C", toplevel, "apply", "--check", "--whitespace=error-all", "-"}
	command := exec.CommandContext(ctx, "git", arguments...)
	command.Stdin = strings.NewReader(patch)
	checkOutput, checkErr := command.CombinedOutput()
	if checkErr != nil {
		return map[string]any{"ok": false, "error_kind": "patch_apply_error", "error": strings.TrimSpace(string(checkOutput)), "dry_run": dryRun}
	}
	if !dryRun {
		command = exec.CommandContext(ctx, "git", "-C", toplevel, "apply", "--whitespace=error-all", "-")
		command.Stdin = strings.NewReader(patch)
		output, applyErr := command.CombinedOutput()
		if applyErr != nil {
			return map[string]any{"ok": false, "error_kind": "patch_apply_error", "error": strings.TrimSpace(string(output)), "dry_run": false}
		}
	}
	return map[string]any{"ok": true, "dry_run": dryRun, "applied": !dryRun, "patch_bytes": len(patch)}
}

func (e *Engine) updateMission(args map[string]any) map[string]any {
	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	section := stringArg(args, "section_title", "Current Mission")
	content := stringArg(args, "content", "")
	if chunks := stringSliceArg(args, "chunks"); len(chunks) > 0 {
		content = strings.Join(chunks, "\n")
	}
	if stringArg(args, "preset", "") == "mandatory_system_tool_log" {
		content = "## Mandatory System Tool Log\n\n" + content
	}
	path := "docs/CURRENT_TASK.md"
	if _, err := os.Stat(filepath.Join(e.settings.ProjectRoot, path)); err != nil {
		path = "CURRENT_TASK.md"
	}
	absolute, oldData, err := e.loadWritable(path, true)
	if err != nil {
		return withError("mission_update_failed", err)
	}
	block := fmt.Sprintf("## %s\n\n%s\n", strings.TrimLeft(section, "# "), content)
	old := string(oldData)
	newText := old
	if strings.TrimSpace(old) == "" {
		newText = block
	} else if strings.Contains(old, "## Goal") && stringArg(args, "position", "before_goal") == "before_goal" {
		newText = strings.Replace(old, "## Goal", block+"\n## Goal", 1)
	} else {
		if !strings.HasSuffix(newText, "\n") {
			newText += "\n"
		}
		newText += "\n" + block
	}
	return e.applyText(absolute, oldData, newText, nil, dryRun)
}

func unifiedDiff(path, oldText, newText string, limit int) string {
	if oldText == newText {
		return ""
	}
	oldLines := strings.Split(strings.TrimSuffix(oldText, "\n"), "\n")
	newLines := strings.Split(strings.TrimSuffix(newText, "\n"), "\n")
	var builder strings.Builder
	fmt.Fprintf(&builder, "--- a/%s\n+++ b/%s\n@@ -1,%d +1,%d @@\n", path, path, len(oldLines), len(newLines))
	for _, line := range oldLines {
		builder.WriteString("-")
		builder.WriteString(line)
		builder.WriteString("\n")
	}
	for _, line := range newLines {
		builder.WriteString("+")
		builder.WriteString(line)
		builder.WriteString("\n")
	}
	result, _ := capText(builder.String(), limit)
	return result
}
