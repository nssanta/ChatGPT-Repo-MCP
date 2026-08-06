package tools

import (
	"bufio"
	"context"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

var ignoredDirectories = map[string]bool{
	".git": true, "node_modules": true, ".venv": true, "venv": true,
	"__pycache__": true, ".mypy_cache": true, ".pytest_cache": true,
	".tox": true, "dist": true, "build": true, ".idea": true, ".vscode": true,
}

var stackPresets = map[string]map[string]string{
	"go":     {"test": "go test ./...", "lint": "go vet ./...", "build": "go build ./...", "format": "gofmt -l ."},
	"python": {"test": "pytest -x -q", "lint": "ruff check .", "typecheck": "mypy .", "format": "ruff format --check ."},
	"node":   {"test": "npm test", "lint": "npm run lint --if-present", "typecheck": "npx tsc --noEmit", "build": "npm run build --if-present"},
	"rust":   {"test": "cargo test", "lint": "cargo clippy", "build": "cargo build", "format": "cargo fmt --check"},
}

func (e *Engine) resolveDirectory(path string, allowHidden bool) (string, error) {
	resolved, err := e.perimeter.Resolve(path, allowHidden, false)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(resolved.Absolute)
	if err != nil {
		return "", err
	}
	if !info.IsDir() {
		return "", fmt.Errorf("not a directory: %s", path)
	}
	return resolved.Absolute, nil
}

func (e *Engine) resolveRepo(ctx context.Context, repo string) (string, error) {
	start := e.settings.ProjectRoot
	if strings.TrimSpace(repo) != "" {
		var err error
		start, err = e.resolveDirectory(repo, true)
		if err != nil {
			return "", err
		}
	}
	result := runProcess(ctx, start, 15*time.Second, nil, "git", "-C", start, "rev-parse", "--show-toplevel")
	if result.ExitCode != 0 || result.TimedOut {
		return "", fmt.Errorf("not a git repository: %s", repo)
	}
	toplevel := strings.TrimSpace(result.Stdout)
	if _, err := e.perimeter.Resolve(toplevel, true, false); err != nil {
		return "", fmt.Errorf("git repository escapes workspace: %w", err)
	}
	return toplevel, nil
}

func detectStack(directory string) []string {
	var stacks []string
	exists := func(name string) bool {
		_, err := os.Stat(filepath.Join(directory, name))
		return err == nil
	}
	if exists("go.mod") {
		stacks = append(stacks, "go")
	}
	if exists("pyproject.toml") || exists("setup.py") || exists("requirements.txt") || exists("Pipfile") {
		stacks = append(stacks, "python")
	}
	if exists("package.json") {
		stacks = append(stacks, "node")
		if exists("tsconfig.json") {
			stacks = append(stacks, "ts")
		}
	}
	if exists("Cargo.toml") {
		stacks = append(stacks, "rust")
	}
	if exists("Makefile") || exists("makefile") {
		stacks = append(stacks, "make")
	}
	if exists("Dockerfile") {
		stacks = append(stacks, "docker")
	}
	return stacks
}

func makefileTargets(directory string) []string {
	path := filepath.Join(directory, "Makefile")
	file, err := os.Open(path)
	if err != nil {
		file, err = os.Open(filepath.Join(directory, "makefile"))
	}
	if err != nil {
		return nil
	}
	defer file.Close()
	seen := make(map[string]bool)
	var targets []string
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := scanner.Text()
		if line == "" || line[0] == ' ' || line[0] == '\t' || strings.HasPrefix(strings.TrimSpace(line), "#") || strings.HasPrefix(line, ".") {
			continue
		}
		name, _, ok := strings.Cut(line, ":")
		name = strings.TrimSpace(name)
		if !ok || name == "" || strings.ContainsAny(name, " =$%") || seen[name] {
			continue
		}
		seen[name] = true
		targets = append(targets, name)
	}
	sort.Strings(targets)
	return targets
}

func (e *Engine) workspaceEntries(ctx context.Context) []map[string]any {
	root := e.settings.ProjectRoot
	if hasGitMarker(root) {
		return []map[string]any{e.repoEntry(ctx, root, "", true)}
	}
	var found []map[string]any
	_ = filepath.WalkDir(root, func(path string, entry os.DirEntry, err error) error {
		if err != nil || path == root {
			return nil
		}
		rel, relErr := filepath.Rel(root, path)
		if relErr != nil {
			return filepath.SkipDir
		}
		depth := len(strings.Split(filepath.ToSlash(rel), "/"))
		if entry.IsDir() && (ignoredDirectories[entry.Name()] || strings.HasPrefix(entry.Name(), ".")) {
			return filepath.SkipDir
		}
		if entry.IsDir() && depth > e.settings.WorkspaceScanDepth {
			return filepath.SkipDir
		}
		if entry.IsDir() && hasGitMarker(path) {
			found = append(found, e.repoEntry(ctx, path, filepath.ToSlash(rel), true))
			return filepath.SkipDir
		}
		return nil
	})
	if len(found) > 0 {
		sort.Slice(found, func(i, j int) bool { return fmt.Sprint(found[i]["path"]) < fmt.Sprint(found[j]["path"]) })
		return found
	}
	entries, _ := os.ReadDir(root)
	for _, entry := range entries {
		if !entry.IsDir() || ignoredDirectories[entry.Name()] || strings.HasPrefix(entry.Name(), ".") {
			continue
		}
		path := filepath.Join(root, entry.Name())
		found = append(found, e.repoEntry(ctx, path, entry.Name(), false))
	}
	return found
}

func (e *Engine) repoEntry(ctx context.Context, directory, relative string, isGit bool) map[string]any {
	entry := map[string]any{
		"path": relative, "stack": detectStack(directory), "is_git": isGit,
		"makefile_targets": makefileTargets(directory),
	}
	if isGit {
		if result := runProcess(ctx, directory, 5*time.Second, nil, "git", "-C", directory, "branch", "--show-current"); result.ExitCode == 0 {
			entry["branch"] = strings.TrimSpace(result.Stdout)
		}
		if result := runProcess(ctx, directory, 5*time.Second, nil, "git", "-C", directory, "status", "--porcelain"); result.ExitCode == 0 {
			entry["dirty"] = len(strings.TrimSpace(result.Stdout)) > 0
		}
	}
	return entry
}

func hasGitMarker(directory string) bool {
	_, err := os.Lstat(filepath.Join(directory, ".git"))
	return err == nil
}

func (e *Engine) presetsFor(path string) map[string]any {
	directory, err := e.resolveDirectory(firstNonEmpty(path, "."), true)
	if err != nil {
		return map[string]any{}
	}
	result := make(map[string]any)
	for _, stack := range detectStack(directory) {
		for action, command := range stackPresets[stack] {
			if _, exists := result[action]; !exists {
				result[action] = map[string]any{"command": command, "cwd": e.perimeter.Display(directory), "parser": "auto"}
			}
		}
	}
	targets := makefileTargets(directory)
	for _, action := range []string{"test", "lint", "typecheck", "format", "build"} {
		if containsString(targets, action) {
			result[action] = map[string]any{"command": "make " + action, "cwd": e.perimeter.Display(directory), "parser": "auto"}
		}
	}
	return result
}
