package tools

import (
	"context"
	"os"
	"path/filepath"
	"runtime"
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

// resolveExecutable uses the same explicit-first PATH contract exposed by
// doctor and inherited by child processes. It intentionally never searches a
// repository-local virtual environment: the standalone server stays portable.
func (e *Engine) resolveExecutable(name string) (string, string) {
	entries, _ := e.effectivePath()
	candidates := []string{name}
	if runtime.GOOS == "windows" && filepath.Ext(name) == "" {
		candidates = append(candidates, name+".exe", name+".cmd", name+".bat")
	}
	for _, entry := range entries {
		for _, candidateName := range candidates {
			candidate := filepath.Join(entry.Path, candidateName)
			if info, err := os.Stat(candidate); err == nil && !info.IsDir() && info.Mode()&0o111 != 0 {
				return candidate, entry.Source
			}
		}
	}
	return "", "not_found"
}

func (e *Engine) toolStatus(name string) map[string]any {
	path, source := e.resolveExecutable(name)
	result := map[string]any{"name": name, "available": path != "", "path": nil, "source": "not_found", "version": nil, "version_error": nil}
	if path == "" {
		return result
	}
	result["path"], result["source"] = path, source
	args := []string{"--version"}
	if name == "go" {
		args = []string{"version"}
	}
	process := runProcess(context.Background(), e.settings.ProjectRoot, 3*time.Second, e.commandEnvironment(nil), path, args...)
	line := strings.Split(strings.TrimSpace(process.Stdout+"\n"+process.Stderr), "\n")[0]
	if process.ExitCode != 0 {
		result["version_error"] = firstNonEmpty(line, "version command failed")
	} else {
		result["version"] = line
	}
	return result
}
