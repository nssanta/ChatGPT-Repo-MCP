package tools

import (
	"bufio"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

func (e *Engine) executeReadTool(ctx context.Context, name string, args map[string]any) map[string]any {
	switch name {
	case "repo_info":
		return e.repoInfo(ctx, stringArg(args, "repo", ""))
	case "list_dir":
		return e.listDirectory(stringArg(args, "path", "."), boolArg(args, "include_hidden", true), intArg(args, "limit", 200))
	case "tree":
		return e.directoryTree(stringArg(args, "path", "."), intArg(args, "depth", 4), boolArg(args, "include_hidden", true))
	case "read_text_file":
		return e.readText(stringArg(args, "path", ""), intArg(args, "start_line", 1), intArg(args, "end_line", 0), boolArg(args, "with_line_numbers", true))
	case "read_multiple_files":
		return e.readMultiple(stringSliceArg(args, "paths"))
	case "file_metadata":
		return e.fileMetadata(stringArg(args, "path", ""), boolArg(args, "include_stat", true))
	case "find_files":
		return e.findFiles(stringArg(args, "pattern", "*"), stringArg(args, "path", "."), boolArg(args, "include_hidden", true), intArg(args, "limit", 200))
	case "search_text":
		return e.searchText(ctx, stringArg(args, "query", ""), stringArg(args, "path", "."), stringSliceArg(args, "paths"), boolArg(args, "regex", false), boolArg(args, "case_sensitive", true), intArg(args, "limit", 100))
	case "symbol_search":
		return e.searchText(ctx, `\b`+regexp.QuoteMeta(stringArg(args, "symbol", ""))+`\b`, stringArg(args, "path", "."), stringSliceArg(args, "paths"), true, true, intArg(args, "limit", 100))
	case "recent_changes":
		return e.recentChanges(ctx, stringArg(args, "path", "."), stringSliceArg(args, "paths"), intArg(args, "limit", 100))
	case "todo_scan":
		return e.searchText(ctx, `\b(TODO|FIXME|HACK|XXX)\b`, stringArg(args, "path", "."), stringSliceArg(args, "paths"), true, false, intArg(args, "limit", 100))
	case "dependency_map":
		return e.dependencyMap(stringArg(args, "path", "."))
	case "list_repos":
		entries := e.workspaceEntries(ctx)
		return map[string]any{"ok": true, "repos": entries, "count": len(entries)}
	case "doctor":
		return e.doctor(ctx)
	case "smoke_all":
		return e.smokeAll(ctx)
	case "context_bootstrap":
		return e.contextBootstrap(ctx)
	case "batch_call":
		return e.batchCall(ctx, args)
	case "code_diagnostics":
		return e.codeDiagnostics(ctx, args)
	case "symbol_definition":
		return e.symbolDefinition(ctx, args)
	case "document_symbols":
		return e.documentSymbols(stringArg(args, "path", ""))
	case "workspace_symbols":
		return e.workspaceSymbols(ctx, args)
	default:
		return failure("unknown_read_tool", name)
	}
}

func (e *Engine) repoInfo(ctx context.Context, repo string) map[string]any {
	result := map[string]any{
		"ok": true, "project_root": e.settings.ProjectRoot, "exists": true, "is_dir": true,
		"implementation": "go",
		"config": map[string]any{
			"access_mode": e.settings.AccessMode, "full_access": e.settings.FullAccess(),
			"default_dry_run": !e.settings.FullAccess(), "filesystem_unrestricted": e.settings.FilesystemUnrestricted,
			"allow_secret_access": e.settings.AllowSecretAccess, "transport": e.settings.Transport,
			"max_file_bytes": e.settings.MaxFileBytes, "max_response_chars": e.settings.MaxResponseChars,
			"max_read_files": e.settings.MaxReadFiles, "max_search_results": e.settings.MaxSearchResults,
			"max_tree_entries": e.settings.MaxTreeEntries, "blocked_globs": e.settings.BlockedGlobs,
		},
	}
	if toplevel, err := e.resolveRepo(ctx, repo); err == nil {
		result["git"] = e.gitInfo(ctx, toplevel)
	} else {
		result["git_error"] = err.Error()
		entries := e.workspaceEntries(ctx)
		if len(entries) > 0 {
			result["git"] = map[string]any{"polyrepo": true, "repos": entries}
		}
	}
	return result
}

func (e *Engine) listDirectory(path string, includeHidden bool, limit int) map[string]any {
	resolved, err := e.perimeter.Resolve(path, includeHidden, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	entries, err := os.ReadDir(resolved.Absolute)
	if err != nil {
		return withError("list_failed", err)
	}
	sort.Slice(entries, func(i, j int) bool {
		if entries[i].IsDir() != entries[j].IsDir() {
			return entries[i].IsDir()
		}
		return strings.ToLower(entries[i].Name()) < strings.ToLower(entries[j].Name())
	})
	limit = min(max(limit, 0), e.settings.MaxTreeEntries)
	result := make([]map[string]any, 0, min(len(entries), limit))
	for _, entry := range entries {
		child := filepath.Join(resolved.Absolute, entry.Name())
		childRel := filepath.ToSlash(filepath.Join(resolved.Relative, entry.Name()))
		if e.perimeter.IsBlocked(childRel) || (!includeHidden && strings.HasPrefix(entry.Name(), ".")) {
			continue
		}
		if _, resolveErr := e.perimeter.Resolve(child, includeHidden, false); resolveErr != nil {
			continue
		}
		if len(result) >= limit {
			break
		}
		info, infoErr := entry.Info()
		item := map[string]any{"name": entry.Name(), "path": e.perimeter.Display(child)}
		if entry.Type()&os.ModeSymlink != 0 {
			item["type"] = "symlink"
		} else if entry.IsDir() {
			item["type"] = "dir"
		} else {
			item["type"] = "file"
		}
		if infoErr == nil && !entry.IsDir() {
			item["size"] = info.Size()
		} else {
			item["size"] = nil
		}
		result = append(result, item)
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(resolved.Absolute), "entries": result, "truncated": len(result) < len(entries)}
}

func (e *Engine) directoryTree(path string, depth int, includeHidden bool) map[string]any {
	resolved, err := e.perimeter.Resolve(path, includeHidden, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	if depth < 0 {
		depth = 0
	}
	lines := []string{filepath.Base(resolved.Absolute) + "/"}
	count := 0
	var walk func(string, string, int)
	walk = func(directory, prefix string, remaining int) {
		if remaining == 0 || count >= e.settings.MaxTreeEntries {
			return
		}
		entries, readErr := os.ReadDir(directory)
		if readErr != nil {
			return
		}
		sort.Slice(entries, func(i, j int) bool { return entries[i].Name() < entries[j].Name() })
		visible := make([]os.DirEntry, 0, len(entries))
		for _, entry := range entries {
			child := filepath.Join(directory, entry.Name())
			if (!includeHidden && strings.HasPrefix(entry.Name(), ".")) || e.perimeter.IsBlocked(e.perimeter.Display(child)) {
				continue
			}
			if _, resolveErr := e.perimeter.Resolve(child, includeHidden, false); resolveErr == nil {
				visible = append(visible, entry)
			}
		}
		for index, entry := range visible {
			if count >= e.settings.MaxTreeEntries {
				return
			}
			last := index == len(visible)-1
			connector := "├── "
			nextPrefix := prefix + "│   "
			if last {
				connector = "└── "
				nextPrefix = prefix + "    "
			}
			suffix := ""
			if entry.IsDir() {
				suffix = "/"
			}
			lines = append(lines, prefix+connector+entry.Name()+suffix)
			count++
			if entry.IsDir() {
				walk(filepath.Join(directory, entry.Name()), nextPrefix, remaining-1)
			}
		}
	}
	walk(resolved.Absolute, "", depth)
	return map[string]any{"ok": true, "path": e.perimeter.Display(resolved.Absolute), "tree": strings.Join(lines, "\n"), "entries": count, "truncated": count >= e.settings.MaxTreeEntries}
}

func (e *Engine) readText(path string, startLine, endLine int, withNumbers bool) map[string]any {
	resolved, err := e.perimeter.Resolve(path, true, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	data, err := os.ReadFile(resolved.Absolute)
	if err != nil {
		return withError("read_failed", err)
	}
	if int64(len(data)) > e.settings.MaxFileBytes {
		return failure("file_too_large", fmt.Sprintf("file exceeds MAX_FILE_BYTES (%d > %d)", len(data), e.settings.MaxFileBytes))
	}
	if strings.IndexByte(string(data), 0) >= 0 {
		return failure("binary_file", "file is not UTF-8 text")
	}
	text := strings.ReplaceAll(string(data), "\r\n", "\n")
	lines := strings.Split(text, "\n")
	if len(lines) > 0 && lines[len(lines)-1] == "" {
		lines = lines[:len(lines)-1]
	}
	if startLine < 1 {
		startLine = 1
	}
	if endLine <= 0 || endLine > len(lines) {
		endLine = len(lines)
	}
	if startLine > endLine+1 {
		return failure("invalid_line_range", "start_line exceeds end_line")
	}
	selected := []string{}
	if startLine <= len(lines) && startLine <= endLine {
		selected = append(selected, lines[startLine-1:endLine]...)
	}
	if withNumbers {
		width := len(strconv.Itoa(max(endLine, 1)))
		for index := range selected {
			selected[index] = fmt.Sprintf("%*d: %s", width, startLine+index, selected[index])
		}
	}
	content, truncated := capText(strings.Join(selected, "\n"), e.settings.MaxResponseChars)
	digest := sha256.Sum256(data)
	return map[string]any{
		"ok": true, "path": e.perimeter.Display(resolved.Absolute), "content": content,
		"sha256": hex.EncodeToString(digest[:]), "start_line": startLine, "end_line": endLine,
		"total_lines": len(lines), "truncated": truncated,
	}
}

func (e *Engine) readMultiple(paths []string) map[string]any {
	if len(paths) > e.settings.MaxReadFiles {
		return failure("too_many_files", fmt.Sprintf("requested %d files; maximum is %d", len(paths), e.settings.MaxReadFiles))
	}
	files := make([]map[string]any, 0, len(paths))
	for _, path := range paths {
		files = append(files, e.readText(path, 1, 0, false))
	}
	return map[string]any{"ok": true, "files": files, "count": len(files)}
}

func (e *Engine) fileMetadata(path string, includeStat bool) map[string]any {
	resolved, err := e.perimeter.Resolve(path, true, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	info, err := os.Lstat(resolved.Absolute)
	if err != nil {
		return withError("stat_failed", err)
	}
	result := map[string]any{
		"ok": true, "path": e.perimeter.Display(resolved.Absolute), "name": info.Name(),
		"type": fileType(info), "size": info.Size(), "mode": info.Mode().String(),
		"modified": info.ModTime().UTC().Format(time.RFC3339Nano),
	}
	if info.Mode().IsRegular() && info.Size() <= e.settings.MaxFileBytes {
		if data, readErr := os.ReadFile(resolved.Absolute); readErr == nil {
			digest := sha256.Sum256(data)
			result["sha256"] = hex.EncodeToString(digest[:])
		}
	}
	if !includeStat {
		delete(result, "mode")
		delete(result, "modified")
	}
	return result
}

func fileType(info os.FileInfo) string {
	if info.Mode()&os.ModeSymlink != 0 {
		return "symlink"
	}
	if info.IsDir() {
		return "dir"
	}
	return "file"
}

func (e *Engine) findFiles(pattern, path string, includeHidden bool, limit int) map[string]any {
	resolved, err := e.perimeter.Resolve(path, includeHidden, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	limit = min(max(limit, 0), e.settings.MaxSearchResults)
	var matches []map[string]any
	truncated := false
	_ = filepath.WalkDir(resolved.Absolute, func(current string, entry os.DirEntry, walkErr error) error {
		if walkErr != nil {
			return nil
		}
		if current != resolved.Absolute && entry.IsDir() && (ignoredDirectories[entry.Name()] || (!includeHidden && strings.HasPrefix(entry.Name(), "."))) {
			return filepath.SkipDir
		}
		if current == resolved.Absolute || entry.IsDir() {
			return nil
		}
		display := e.perimeter.Display(current)
		if e.perimeter.IsBlocked(display) {
			return nil
		}
		matched, matchErr := filepath.Match(pattern, entry.Name())
		if matchErr != nil {
			return filepath.SkipAll
		}
		if !matched {
			matched, _ = filepath.Match(pattern, filepath.ToSlash(display))
		}
		if matched {
			if len(matches) >= limit {
				truncated = true
				return filepath.SkipAll
			}
			info, _ := entry.Info()
			item := map[string]any{"path": display, "name": entry.Name()}
			if info != nil {
				item["size"] = info.Size()
			}
			matches = append(matches, item)
		}
		return nil
	})
	return map[string]any{"ok": true, "pattern": pattern, "matches": matches, "count": len(matches), "truncated": truncated}
}

func (e *Engine) searchText(ctx context.Context, query, path string, paths []string, regexMode, caseSensitive bool, limit int) map[string]any {
	if query == "" {
		return failure("invalid_query", "query must not be empty")
	}
	limit = min(max(limit, 0), e.settings.MaxSearchResults)
	targets := paths
	if len(targets) == 0 {
		targets = []string{path}
	}
	resolvedTargets := make([]string, 0, len(targets))
	for _, target := range targets {
		resolved, err := e.perimeter.Resolve(target, true, false)
		if err != nil {
			return withError("path_not_allowed", err)
		}
		resolvedTargets = append(resolvedTargets, resolved.Absolute)
	}
	if _, err := exec.LookPath("rg"); err == nil {
		return e.searchWithRipgrep(ctx, query, resolvedTargets, regexMode, caseSensitive, limit)
	}
	return e.searchFallback(query, resolvedTargets, regexMode, caseSensitive, limit)
}

func (e *Engine) searchWithRipgrep(ctx context.Context, query string, targets []string, regexMode, caseSensitive bool, limit int) map[string]any {
	arguments := []string{"--json", "--line-number", "--no-messages"}
	if !regexMode {
		arguments = append(arguments, "--fixed-strings")
	}
	if !caseSensitive {
		arguments = append(arguments, "--ignore-case")
	}
	for _, pattern := range e.settings.BlockedGlobs {
		arguments = append(arguments, "--glob", "!"+pattern)
	}
	arguments = append(arguments, query)
	arguments = append(arguments, targets...)
	command := exec.CommandContext(ctx, "rg", arguments...)
	output, err := command.Output()
	if err != nil {
		if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 1 {
			return withError("search_failed", err)
		}
	}
	results := make([]map[string]any, 0, min(limit, 64))
	scanner := bufio.NewScanner(strings.NewReader(string(output)))
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() && len(results) < limit {
		var event struct {
			Type string `json:"type"`
			Data struct {
				Path struct {
					Text string `json:"text"`
				} `json:"path"`
				Lines struct {
					Text string `json:"text"`
				} `json:"lines"`
				LineNumber int `json:"line_number"`
			} `json:"data"`
		}
		if json.Unmarshal(scanner.Bytes(), &event) != nil || event.Type != "match" {
			continue
		}
		results = append(results, map[string]any{
			"path": e.perimeter.Display(event.Data.Path.Text), "line": event.Data.LineNumber,
			"text": strings.TrimSuffix(event.Data.Lines.Text, "\n"),
		})
	}
	return map[string]any{"ok": true, "query": query, "matches": results, "count": len(results), "truncated": len(results) >= limit, "engine": "ripgrep"}
}

func (e *Engine) searchFallback(query string, targets []string, regexMode, caseSensitive bool, limit int) map[string]any {
	expression := regexp.QuoteMeta(query)
	if regexMode {
		expression = query
	}
	if !caseSensitive {
		expression = "(?i)" + expression
	}
	compiled, err := regexp.Compile(expression)
	if err != nil {
		return withError("invalid_regex", err)
	}
	var results []map[string]any
	for _, target := range targets {
		_ = filepath.WalkDir(target, func(path string, entry os.DirEntry, walkErr error) error {
			if walkErr != nil || len(results) >= limit {
				return filepath.SkipAll
			}
			if entry.IsDir() {
				if path != target && ignoredDirectories[entry.Name()] {
					return filepath.SkipDir
				}
				return nil
			}
			if e.perimeter.IsBlocked(e.perimeter.Display(path)) {
				return nil
			}
			file, openErr := os.Open(path)
			if openErr != nil {
				return nil
			}
			defer file.Close()
			scanner := bufio.NewScanner(io.LimitReader(file, e.settings.MaxFileBytes))
			line := 0
			for scanner.Scan() && len(results) < limit {
				line++
				if compiled.MatchString(scanner.Text()) {
					results = append(results, map[string]any{"path": e.perimeter.Display(path), "line": line, "text": scanner.Text()})
				}
			}
			return nil
		})
	}
	return map[string]any{"ok": true, "query": query, "matches": results, "count": len(results), "truncated": len(results) >= limit, "engine": "go-fallback"}
}

func (e *Engine) recentChanges(ctx context.Context, path string, paths []string, limit int) map[string]any {
	result := e.searchText(ctx, ".", path, paths, true, true, e.settings.MaxSearchResults)
	matches, _ := result["matches"].([]map[string]any)
	sort.SliceStable(matches, func(i, j int) bool {
		left, leftErr := os.Stat(filepath.Join(e.settings.ProjectRoot, fmt.Sprint(matches[i]["path"])))
		right, rightErr := os.Stat(filepath.Join(e.settings.ProjectRoot, fmt.Sprint(matches[j]["path"])))
		if leftErr != nil || rightErr != nil {
			return false
		}
		return left.ModTime().After(right.ModTime())
	})
	if len(matches) > limit {
		matches = matches[:limit]
	}
	return map[string]any{"ok": true, "changes": matches, "count": len(matches)}
}

func (e *Engine) dependencyMap(path string) map[string]any {
	directory, err := e.resolveDirectory(path, true)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	dependencies := make(map[string]any)
	if data, readErr := os.ReadFile(filepath.Join(directory, "package.json")); readErr == nil {
		var packageJSON map[string]any
		if json.Unmarshal(data, &packageJSON) == nil {
			dependencies["node"] = map[string]any{"dependencies": packageJSON["dependencies"], "devDependencies": packageJSON["devDependencies"]}
		}
	}
	if file, openErr := os.Open(filepath.Join(directory, "go.mod")); openErr == nil {
		defer file.Close()
		var modules []string
		scanner := bufio.NewScanner(file)
		for scanner.Scan() {
			line := strings.TrimSpace(scanner.Text())
			fields := strings.Fields(line)
			if len(fields) >= 2 && fields[0] != "module" && fields[0] != "go" && fields[0] != "require" && !strings.HasPrefix(fields[0], "//") {
				modules = append(modules, fields[0])
			}
		}
		dependencies["go"] = modules
	}
	if data, readErr := os.ReadFile(filepath.Join(directory, "requirements.txt")); readErr == nil {
		var requirements []string
		for _, line := range strings.Split(string(data), "\n") {
			line = strings.TrimSpace(line)
			if line != "" && !strings.HasPrefix(line, "#") {
				requirements = append(requirements, line)
			}
		}
		dependencies["python"] = requirements
	} else if _, statErr := os.Stat(filepath.Join(directory, "pyproject.toml")); statErr == nil {
		dependencies["python"] = map[string]any{"manifest": "pyproject.toml"}
	}
	if _, statErr := os.Stat(filepath.Join(directory, "Cargo.toml")); statErr == nil {
		dependencies["rust"] = map[string]any{"manifest": "Cargo.toml"}
	}
	return map[string]any{"ok": true, "path": e.perimeter.Display(directory), "stack": detectStack(directory), "dependencies": dependencies}
}

func (e *Engine) doctor(ctx context.Context) map[string]any {
	capabilities := map[string]any{}
	for _, name := range []string{"git", "rg", "gh", "ctags", "bash", "go", "python3", "node", "ruff", "mypy", "pyright"} {
		capabilities[name] = e.toolStatus(name)
	}
	entries, warnings := e.effectivePath()
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		paths = append(paths, entry.Path)
	}
	ptyAvailable := runtime.GOOS != "windows"
	capabilities["pty"] = map[string]any{
		"available": ptyAvailable, "enabled": ptyAvailable && e.settings.FullAccess() && e.settings.EnablePTY,
		"reason": func() any {
			if ptyAvailable {
				return nil
			}
			return "POSIX PTY is unavailable on this platform"
		}(),
	}
	capabilities["subagents"] = map[string]any{"available": false, "enabled": false, "reason": "No executor configured in V1"}
	return map[string]any{
		"ok": true, "implementation": "go", "tool_count": len(e.toolNames), "tools": e.toolNames,
		"namespace": e.settings.CanonicalNamespace, "access_mode": e.settings.AccessMode,
		"capabilities": capabilities, "effective_path": paths, "path_warnings": warnings,
		"toolchains": []any{capabilities["go"], capabilities["python3"], capabilities["node"]},
		"repos":      e.workspaceEntries(ctx),
	}
}

func (e *Engine) smokeAll(ctx context.Context) map[string]any {
	checks := []map[string]any{
		e.repoInfo(ctx, ""), e.listDirectory(".", true, 5), e.directoryTree(".", 1, true),
	}
	ok := true
	for _, check := range checks {
		if value, exists := check["ok"]; exists && value == false {
			ok = false
		}
	}
	return map[string]any{"ok": ok, "checks": checks, "tool_count": len(e.toolNames)}
}

func (e *Engine) contextBootstrap(ctx context.Context) map[string]any {
	result := map[string]any{"ok": true, "repo": e.repoInfo(ctx, ""), "repos": e.workspaceEntries(ctx)}
	for _, candidate := range []string{"AGENTS.md", "README.md", "README_RU.md", "docs/CURRENT_TASK.md", "CURRENT_TASK.md"} {
		if metadata := e.fileMetadata(candidate, false); metadata["ok"] == true {
			result["context_file"] = e.readText(candidate, 1, 200, false)
			break
		}
	}
	return result
}

func (e *Engine) batchCall(ctx context.Context, args map[string]any) map[string]any {
	calls := mapsArg(args, "calls")
	if len(calls) > 10 {
		return failure("too_many_calls", "batch_call accepts at most 10 calls")
	}
	results := make([]map[string]any, len(calls))
	invoke := func(index int, call map[string]any) {
		name := stringArg(call, "tool", "")
		arguments := mapArg(call, "args")
		allowed := map[string]bool{"repo_info": true, "list_dir": true, "tree": true, "read_text_file": true, "read_multiple_files": true, "file_metadata": true, "find_files": true, "search_text": true, "symbol_search": true, "recent_changes": true, "todo_scan": true, "dependency_map": true, "git_status": true, "git_diff": true, "git_log": true, "git_show": true, "git_branches": true, "git_blame": true, "git_grep": true}
		if !allowed[name] {
			preview := map[string]bool{"replace_text_in_file": true, "replace_lines": true, "insert_before_heading": true, "batch_edit_files": true}
			if !preview[name] || !boolArg(arguments, "dry_run", false) {
				results[index] = map[string]any{"index": index, "tool": name, "ok": false, "error": "tool is not allowed for batch_call"}
				return
			}
		}
		value := e.Execute(ctx, name, arguments)
		ok := value["ok"] != false
		results[index] = map[string]any{"index": index, "tool": name, "ok": ok, "result": value}
	}
	execution := stringArg(args, "execution", "parallel")
	maximum := intArg(args, "max_concurrency", 4)
	if maximum < 1 {
		maximum = 1
	}
	if maximum > 10 {
		maximum = 10
	}
	if execution == "sequential" {
		for index, call := range calls {
			invoke(index, call)
		}
	} else {
		semaphore := make(chan struct{}, maximum)
		var wait sync.WaitGroup
		for index, call := range calls {
			wait.Add(1)
			go func(index int, call map[string]any) {
				defer wait.Done()
				semaphore <- struct{}{}
				defer func() { <-semaphore }()
				invoke(index, call)
			}(index, call)
		}
		wait.Wait()
	}
	ok := true
	for _, result := range results {
		if result["ok"] == false {
			ok = false
		}
	}
	return map[string]any{"ok": ok, "execution": execution, "max_concurrency": maximum, "results": results, "count": len(results)}
}

func (e *Engine) codeDiagnostics(ctx context.Context, args map[string]any) map[string]any {
	repo, err := e.resolveRepo(ctx, stringArg(args, "repo", ""))
	if err != nil {
		repo = e.settings.ProjectRoot
	}
	language := stringArg(args, "language", "auto")
	stacks := detectStack(repo)
	if language != "auto" {
		stacks = []string{language}
	}
	var checks []map[string]any
	missing := []map[string]any{}
	for _, stack := range stacks {
		var binary string
		var command []string
		switch stack {
		case "go":
			binary, command = "go", []string{"vet", "./..."}
		case "python":
			if _, lookupErr := exec.LookPath("pyright"); lookupErr == nil {
				binary, command = "pyright", []string{"--outputjson"}
			} else {
				binary, command = "ruff", []string{"check", "--output-format=json", "."}
			}
		case "ts", "node":
			binary, command = "npx", []string{"tsc", "--noEmit", "--pretty", "false"}
		default:
			continue
		}
		if _, lookupErr := exec.LookPath(binary); lookupErr != nil {
			missing = append(missing, map[string]any{"tool": binary, "language": stack})
			continue
		}
		run := runProcess(ctx, repo, e.settings.SubprocessTimeout, nil, binary, command...)
		checks = append(checks, map[string]any{"language": stack, "tool": binary, "exit_code": run.ExitCode, "stdout": run.Stdout, "stderr": run.Stderr, "ok": run.ExitCode == 0})
	}
	return map[string]any{"ok": true, "checks": checks, "missing_tools": missing, "diagnostics": []any{}}
}

func (e *Engine) symbolDefinition(ctx context.Context, args map[string]any) map[string]any {
	symbol := stringArg(args, "symbol", "")
	limit := intArg(args, "limit", 20)
	result := e.searchText(ctx, `\b(?:def|class|func|type|const|var)\s+`+regexp.QuoteMeta(symbol)+`\b`, ".", nil, true, true, limit)
	return map[string]any{"ok": result["ok"], "symbol": symbol, "definitions": result["matches"], "engine": result["engine"]}
}

var symbolLine = regexp.MustCompile(`^\s*(?:def|class|func|type|interface|struct|const|var|function)\s+([A-Za-z_][A-Za-z0-9_]*)`)

func (e *Engine) documentSymbols(path string) map[string]any {
	read := e.readText(path, 1, 0, false)
	content, ok := read["content"].(string)
	if !ok {
		return read
	}
	var symbols []map[string]any
	for index, line := range strings.Split(content, "\n") {
		if match := symbolLine.FindStringSubmatch(line); len(match) > 1 {
			symbols = append(symbols, map[string]any{"name": match[1], "kind": strings.Fields(strings.TrimSpace(line))[0], "line": index + 1, "signature": strings.TrimSpace(line)})
		}
	}
	return map[string]any{"ok": true, "path": read["path"], "symbols": symbols, "engine": "regex"}
}

func (e *Engine) workspaceSymbols(ctx context.Context, args map[string]any) map[string]any {
	query := stringArg(args, "query", "")
	limit := intArg(args, "limit", 50)
	result := e.searchText(ctx, query, ".", nil, false, false, limit)
	return map[string]any{"ok": result["ok"], "query": query, "symbols": result["matches"], "engine": result["engine"]}
}

type processResult struct {
	ExitCode int
	Stdout   string
	Stderr   string
	TimedOut bool
}

func runProcess(parent context.Context, directory string, timeout time.Duration, env []string, binary string, arguments ...string) processResult {
	ctx, cancel := context.WithTimeout(parent, timeout)
	defer cancel()
	command := exec.CommandContext(ctx, binary, arguments...)
	command.Dir = directory
	if env != nil {
		command.Env = env
	}
	var stdout strings.Builder
	var stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	exitCode := 0
	if err != nil {
		exitCode = -1
		if exit, ok := err.(*exec.ExitError); ok {
			exitCode = exit.ExitCode()
		}
	}
	return processResult{ExitCode: exitCode, Stdout: stdout.String(), Stderr: stderr.String(), TimedOut: ctx.Err() == context.DeadlineExceeded}
}
