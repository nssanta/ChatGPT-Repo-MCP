// Package security enforces the shared filesystem perimeter and glob policy.
package security

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
)

// ResolvedPath retains both the physical target and its display path.
type ResolvedPath struct {
	Absolute string
	Root     string
	Relative string
}

// Perimeter resolves user-supplied paths against trusted roots.
type Perimeter struct {
	settings config.Settings
	roots    []string
}

// New constructs a path perimeter from normalized settings.
func New(settings config.Settings) *Perimeter {
	roots := []string{settings.ProjectRoot}
	roots = append(roots, settings.WorkspaceRoots...)
	return &Perimeter{settings: settings, roots: roots}
}

// Roots returns a defensive copy of the configured roots.
func (p *Perimeter) Roots() []string { return append([]string(nil), p.roots...) }

// Resolve validates a structured-tool path. When forWrite is true, the write
// glob policy and secret/binary rules are also applied.
func (p *Perimeter) Resolve(candidate string, allowHidden, forWrite bool) (ResolvedPath, error) {
	if strings.TrimSpace(candidate) == "" {
		candidate = "."
	}
	target := candidate
	if !filepath.IsAbs(target) {
		target = filepath.Join(p.settings.ProjectRoot, target)
	}
	target, err := filepath.Abs(target)
	if err != nil {
		return ResolvedPath{}, fmt.Errorf("resolve path: %w", err)
	}
	target = filepath.Clean(target)
	physical, err := resolvePhysical(target)
	if err != nil {
		return ResolvedPath{}, err
	}

	root := p.settings.ProjectRoot
	if !p.settings.FilesystemUnrestricted {
		root = ""
		for _, allowed := range p.roots {
			if contains(allowed, physical) {
				root = allowed
				break
			}
		}
		if root == "" {
			return ResolvedPath{}, fmt.Errorf("path is outside allowed workspace roots: %s", candidate)
		}
	}
	relative := filepath.ToSlash(physical)
	if root != "" && contains(root, physical) {
		if rel, relErr := filepath.Rel(root, physical); relErr == nil {
			relative = filepath.ToSlash(rel)
		}
	}
	if relative == "" {
		relative = "."
	}
	if !allowHidden && hidden(relative) {
		return ResolvedPath{}, fmt.Errorf("hidden path is not allowed: %s", candidate)
	}
	if p.blocked(relative) {
		return ResolvedPath{}, fmt.Errorf("path is blocked by security policy: %s", candidate)
	}
	if forWrite && !p.writable(relative) {
		return ResolvedPath{}, fmt.Errorf("path is not allowed by WRITABLE_GLOBS: %s", candidate)
	}
	return ResolvedPath{Absolute: physical, Root: root, Relative: relative}, nil
}

// Display returns a stable workspace-relative path where possible.
func (p *Perimeter) Display(path string) string {
	for _, root := range p.roots {
		if contains(root, path) {
			rel, err := filepath.Rel(root, path)
			if err == nil {
				if root == p.settings.ProjectRoot {
					return filepath.ToSlash(rel)
				}
				return filepath.ToSlash(filepath.Join(filepath.Base(root), rel))
			}
		}
	}
	return filepath.ToSlash(path)
}

// IsBlocked reports whether a relative path is unavailable to structured tools.
func (p *Perimeter) IsBlocked(relative string) bool { return p.blocked(filepath.ToSlash(relative)) }

func (p *Perimeter) blocked(relative string) bool {
	secret := matchesAny(relative, p.settings.SecretGlobs)
	if secret && p.settings.AllowSecretAccess {
		return matchesAny(relative, p.settings.BinaryGlobs)
	}
	return matchesAny(relative, p.settings.BlockedGlobs) || secret || matchesAny(relative, p.settings.BinaryGlobs)
}

func (p *Perimeter) writable(relative string) bool {
	if matchesAny(relative, p.settings.SecretGlobs) && !p.settings.AllowSecretAccess {
		return false
	}
	catchAll := false
	for _, pattern := range p.settings.WritableGlobs {
		switch strings.TrimSpace(pattern) {
		case "*", "**", "**/*":
			catchAll = true
			continue
		}
		if globMatch(relative, pattern) {
			return true
		}
	}
	return catchAll && p.settings.DangerouslyAllowAllWrites
}

func resolvePhysical(target string) (string, error) {
	physical, err := filepath.EvalSymlinks(target)
	if err == nil {
		return physical, nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return "", fmt.Errorf("resolve symlinks for %s: %w", target, err)
	}
	parent := filepath.Dir(target)
	resolvedParent, parentErr := filepath.EvalSymlinks(parent)
	if parentErr != nil {
		if errors.Is(parentErr, os.ErrNotExist) {
			return filepath.Clean(target), nil
		}
		return "", fmt.Errorf("resolve parent for %s: %w", target, parentErr)
	}
	return filepath.Join(resolvedParent, filepath.Base(target)), nil
}

func contains(root, target string) bool {
	rel, err := filepath.Rel(filepath.Clean(root), filepath.Clean(target))
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func hidden(relative string) bool {
	for _, part := range strings.Split(filepath.ToSlash(relative), "/") {
		if strings.HasPrefix(part, ".") && part != "." && part != ".." {
			return true
		}
	}
	return false
}

func matchesAny(relative string, patterns []string) bool {
	for _, pattern := range patterns {
		if globMatch(relative, pattern) {
			return true
		}
	}
	return false
}

func globMatch(relative, pattern string) bool {
	relative = strings.TrimPrefix(filepath.ToSlash(relative), "./")
	pattern = strings.TrimPrefix(filepath.ToSlash(strings.TrimSpace(pattern)), "./")
	if pattern == "" {
		return false
	}
	if !strings.Contains(pattern, "/") {
		for _, part := range strings.Split(relative, "/") {
			if simpleGlob(part, pattern) {
				return true
			}
		}
	}
	regex := globRegexp(pattern)
	return regexp.MustCompile(regex).MatchString(relative)
}

func simpleGlob(value, pattern string) bool {
	matched, err := filepath.Match(pattern, value)
	return err == nil && matched
}

func globRegexp(pattern string) string {
	var builder strings.Builder
	builder.WriteString("^")
	for index := 0; index < len(pattern); index++ {
		char := pattern[index]
		switch char {
		case '*':
			if index+1 < len(pattern) && pattern[index+1] == '*' {
				index++
				if index+1 < len(pattern) && pattern[index+1] == '/' {
					index++
					builder.WriteString("(?:.*/)?")
				} else {
					builder.WriteString(".*")
				}
			} else {
				builder.WriteString("[^/]*")
			}
		case '?':
			builder.WriteString("[^/]")
		default:
			builder.WriteString(regexp.QuoteMeta(string(char)))
		}
	}
	builder.WriteString("$")
	return builder.String()
}
