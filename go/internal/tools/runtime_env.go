package tools

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"time"
)

type pathEntry struct {
	Path   string
	Source string
}

func (e *Engine) effectivePath() ([]pathEntry, []string) {
	home, _ := os.UserHomeDir()
	var candidates []pathEntry
	for _, value := range e.settings.MCPExtraPath {
		candidates = append(candidates, pathEntry{value, "explicit_extra"})
	}
	for _, value := range filepath.SplitList(os.Getenv("PATH")) {
		candidates = append(candidates, pathEntry{value, "inherited_path"})
	}
	if value := os.Getenv("VIRTUAL_ENV"); value != "" {
		candidates = append(candidates, pathEntry{filepath.Join(value, "bin"), "virtualenv"})
	}
	for _, item := range []struct{ env, suffix string }{{"GOROOT", "bin"}, {"GOPATH", "bin"}, {"CARGO_HOME", "bin"}, {"PYENV_ROOT", "shims"}} {
		if value := os.Getenv(item.env); value != "" {
			candidates = append(candidates, pathEntry{filepath.Join(value, item.suffix), "standard_fallback"})
		}
	}
	if value := os.Getenv("NVM_BIN"); value != "" {
		candidates = append(candidates, pathEntry{value, "standard_fallback"})
	}
	for _, value := range []string{"/usr/local/go/bin", "/usr/local/bin", "/usr/bin", filepath.Join(home, "go/bin"), filepath.Join(home, ".local/bin"), filepath.Join(home, ".cargo/bin")} {
		candidates = append(candidates, pathEntry{value, "standard_fallback"})
	}
	seen := map[string]bool{}
	entries := make([]pathEntry, 0, len(candidates))
	for _, entry := range candidates {
		if entry.Path == "" {
			continue
		}
		absolute, err := filepath.Abs(entry.Path)
		if err != nil || seen[absolute] {
			continue
		}
		info, err := os.Stat(absolute)
		if err != nil || !info.IsDir() {
			continue
		}
		seen[absolute] = true
		entries = append(entries, pathEntry{absolute, entry.Source})
	}
	if len(entries) == 0 {
		return entries, []string{"effective PATH contains no existing directories"}
	}
	return entries, nil
}

func (e *Engine) commandEnvironment(overrides map[string]string) []string {
	entries, _ := e.effectivePath()
	paths := make([]string, 0, len(entries))
	for _, entry := range entries {
		paths = append(paths, entry.Path)
	}
	environment := make([]string, 0, len(os.Environ())+len(overrides)+1)
	for _, value := range os.Environ() {
		if !strings.HasPrefix(value, "PATH=") {
			environment = append(environment, value)
		}
	}
	environment = append(environment, "PATH="+strings.Join(paths, string(os.PathListSeparator)))
	for key, value := range overrides {
		environment = append(environment, key+"="+value)
	}
	return environment
}

func (e *Engine) toolStatus(name string) map[string]any {
	entries, _ := e.effectivePath()
	var path, source string
	for _, entry := range entries {
		candidate := filepath.Join(entry.Path, name)
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() && info.Mode()&0o111 != 0 {
			path, source = candidate, entry.Source
			break
		}
	}
	result := map[string]any{"name": name, "available": path != "", "path": nil, "source": "not_found", "version": nil, "version_error": nil}
	if path == "" {
		return result
	}
	result["path"], result["source"] = path, source
	args := []string{"--version"}
	if name == "go" {
		args = []string{"version"}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	command := exec.CommandContext(ctx, path, args...)
	command.Env = e.commandEnvironment(nil)
	output, err := command.CombinedOutput()
	line := strings.Split(strings.TrimSpace(string(output)), "\n")[0]
	if err != nil {
		result["version_error"] = firstNonEmpty(line, err.Error())
	} else {
		result["version"] = line
	}
	return result
}
