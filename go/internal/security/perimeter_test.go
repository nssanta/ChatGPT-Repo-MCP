package security

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
)

func perimeterSettings(root string) config.Settings {
	return config.Settings{
		ProjectRoot: root, BlockedGlobs: []string{".env", "**/.git/**", "**/*.bin"},
		SecretGlobs: []string{".env", "**/.git/**"}, BinaryGlobs: []string{"**/*.bin"},
		WritableGlobs: []string{"**/*"}, DangerouslyAllowAllWrites: true,
	}
}

func TestResolveAndPolicies(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "main.go"), []byte("package main\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	p := New(perimeterSettings(root))
	resolved, err := p.Resolve("main.go", true, true)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Relative != "main.go" || p.Display(resolved.Absolute) != "main.go" {
		t.Fatalf("unexpected resolved path: %+v", resolved)
	}
	if _, err := p.Resolve("../escape.txt", true, false); err == nil {
		t.Fatal("expected traversal rejection")
	}
	if _, err := p.Resolve(".env", true, false); err == nil {
		t.Fatal("expected secret rejection")
	}
	if _, err := p.Resolve("file.bin", true, true); err == nil {
		t.Fatal("expected binary rejection")
	}
}

func TestSpecificWritableGlobAndSecretOverride(t *testing.T) {
	root := t.TempDir()
	settings := perimeterSettings(root)
	settings.WritableGlobs = []string{"**/*", "docs/**"}
	settings.DangerouslyAllowAllWrites = false
	p := New(settings)
	if _, err := p.Resolve("docs/new.md", true, true); err != nil {
		t.Fatalf("specific writable glob should win: %v", err)
	}
	if _, err := p.Resolve("src/new.go", true, true); err == nil {
		t.Fatal("catch-all interlock should reject")
	}
	settings.AllowSecretAccess = true
	p = New(settings)
	if _, err := p.Resolve(".env", true, false); err != nil {
		t.Fatalf("full secret access should allow .env: %v", err)
	}
}

func TestSymlinkEscapeRejected(t *testing.T) {
	root, outside := t.TempDir(), t.TempDir()
	if err := os.Symlink(outside, filepath.Join(root, "outside")); err != nil {
		t.Fatal(err)
	}
	p := New(perimeterSettings(root))
	if _, err := p.Resolve("outside/file.txt", true, false); err == nil {
		t.Fatal("expected symlink escape rejection")
	}
}

func TestResolveUsesWorkspaceRootsAndUnrestrictedMode(t *testing.T) {
	root := t.TempDir()
	extra := t.TempDir()
	settings := config.Settings{
		ProjectRoot:               root,
		WorkspaceRoots:            []string{extra},
		BlockedGlobs:              []string{},
		SecretGlobs:               []string{},
		BinaryGlobs:               []string{},
		WritableGlobs:             []string{"**/*"},
		DangerouslyAllowAllWrites: true,
	}
	p := New(settings)
	path := filepath.Join(extra, "shared.txt")
	if err := os.WriteFile(path, []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	resolved, err := p.Resolve(path, true, true)
	if err != nil {
		t.Fatalf("expected workspace-rooted resolve: %v", err)
	}
	if resolved.Root != filepath.Clean(extra) {
		t.Fatalf("root expected %q got %q", filepath.Clean(extra), resolved.Root)
	}

	settings.FilesystemUnrestricted = true
	restricted := New(settings)
	outside := t.TempDir()
	if _, err := restricted.Resolve(filepath.Join(outside, "x.txt"), true, true); err != nil {
		t.Fatalf("unrestricted must allow outside path: %v", err)
	}
}

func TestRootsAndDisplayForRelativeWorkspace(t *testing.T) {
	root := t.TempDir()
	settings := perimeterSettings(root)
	settings.WorkspaceRoots = []string{filepath.Join(root, "child")}
	if err := os.MkdirAll(filepath.Join(root, "child"), 0o755); err != nil {
		t.Fatal(err)
	}
	p := New(settings)
	copyRoots := p.Roots()
	copyRoots[0] = "mutated"
	if len(p.Roots()) != 2 {
		t.Fatalf("roots copy mismatch: %#v", p.Roots())
	}
	if got := p.Display(filepath.Join(root, "a", "b")); got != "a/b" {
		t.Fatalf("display = %q", got)
	}
	if got := p.Display(filepath.Join(root, "child", "x")); got != "child/x" {
		t.Fatalf("display = %q", got)
	}
}

func TestBlockedAndWritableRulesCoverBinarySecretCombinations(t *testing.T) {
	root := t.TempDir()
	settings := perimeterSettings(root)
	settings.BinaryGlobs = []string{"**/*.bin"}
	settings.SecretGlobs = []string{".env"}
	settings.WritableGlobs = []string{"docs/**"}
	settings.DangerouslyAllowAllWrites = true
	p := New(settings)

	if !p.IsBlocked(".env") {
		t.Fatal("secret should be blocked")
	}
	if !p.IsBlocked("foo.bin") {
		t.Fatal("binary should be blocked")
	}
	if p.writable("docs/readme.md") != true {
		t.Fatal("docs path should be writable")
	}
	if p.writable("main.go") != false {
		t.Fatal("unexpected writable path")
	}
}

func TestResolveRejectsHiddenAndSupportsHiddenOverride(t *testing.T) {
	root := t.TempDir()
	settings := perimeterSettings(root)
	settings.WritableGlobs = []string{"**/*"}
	p := New(settings)
	hidden := filepath.Join(root, ".secret", "v.txt")
	if err := os.MkdirAll(filepath.Dir(hidden), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(hidden, []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Resolve(".secret/v.txt", false, true); err == nil {
		t.Fatal("hidden path should be blocked when includeHidden is false")
	}
	if _, err := p.Resolve(".secret/v.txt", true, true); err != nil {
		t.Fatalf("hidden override should pass: %v", err)
	}
}

func TestGlobAndMatchHelpers(t *testing.T) {
	root := t.TempDir()
	_ = New(perimeterSettings(root))
	if match := globMatch("docs/a.txt", "*.txt"); !match {
		t.Fatalf("expected txt match")
	}
	if match := globMatch("docs/a.txt", "**/*.txt"); !match {
		t.Fatalf("expected nested glob match")
	}
	if match := simpleGlob("Go", "Go"); !match {
		t.Fatalf("simple glob mismatch")
	}
	if !strings.Contains(globRegexp("a/**/b"), ".*") {
		t.Fatalf("glob regexp did not compile")
	}

	if !contains(filepath.Clean("/tmp/alpha"), filepath.Clean("/tmp/alpha/file")) {
		t.Fatalf("contains helper mismatch")
	}
	if contains(filepath.Clean("/tmp/alpha"), filepath.Clean("/tmp/beta")) {
		t.Fatal("contains should be false")
	}
}

func TestDisplayOutsideRoots(t *testing.T) {
	root := t.TempDir()
	p := New(perimeterSettings(root))
	outside := filepath.Clean(filepath.Join(root, "..", "outside"))
	if got := p.Display(outside); got != filepath.ToSlash(outside) {
		t.Fatalf("expected raw display path for outside path, got %q", got)
	}
}

func TestResolveBlankCandidateUsesProjectRoot(t *testing.T) {
	root := t.TempDir()
	p := New(perimeterSettings(root))
	resolved, err := p.Resolve("   ", true, true)
	if err != nil {
		t.Fatalf("resolve blank candidate should map to project root: %v", err)
	}
	if resolved.Relative != "." {
		t.Fatalf("expected root-relative dot, got %q", resolved.Relative)
	}
	if resolved.Root != filepath.Clean(root) {
		t.Fatalf("expected root %q got %q", filepath.Clean(root), resolved.Root)
	}
}

func TestResolvePhysicalAndHelpers(t *testing.T) {
	root := t.TempDir()
	settings := perimeterSettings(root)
	p := New(settings)

	missing := filepath.Join(root, "new-file.txt")
	resolvedMissing, err := resolvePhysical(missing)
	if err != nil {
		t.Fatalf("missing-path physical resolve should fallback when parent exists: %v", err)
	}
	if resolvedMissing != filepath.Clean(missing) {
		t.Fatalf("unexpected missing resolve, got %q", resolvedMissing)
	}

	target := filepath.Join(root, "target.txt")
	if err := os.WriteFile(target, []byte("ok"), 0o644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "link")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	resolvedLink, err := resolvePhysical(link)
	if err != nil {
		t.Fatalf("symlink resolve should succeed: %v", err)
	}
	if resolvedLink != filepath.Clean(target) {
		t.Fatalf("expected resolved symlink target %q got %q", target, resolvedLink)
	}

	// error that is neither NotExist should bubble through with wrapped text.
	if _, err := resolvePhysical("\x00"); err == nil {
		t.Fatal("expected invalid path to fail in resolvePhysical")
	}
	missingParent := filepath.Join(root, "deep", "missing.txt")
	if resolvedMissingParent, err := resolvePhysical(missingParent); err != nil {
		t.Fatalf("resolvePhysical with missing parent should fall back to clean path: %v", err)
	} else if resolvedMissingParent != filepath.Clean(missingParent) {
		t.Fatalf("expected clean path for missing parent case, got %q", resolvedMissingParent)
	}

	if resolve := hidden("."); resolve {
		t.Fatal("dot should not be considered hidden")
	}
	if resolve := hidden(".."); resolve {
		t.Fatal("dot-dot should not be considered hidden")
	}
	if !hidden("a/.hidden/file") {
		t.Fatal("expected nested hidden segment")
	}
	if !globMatch("a.txt", "*.txt") {
		t.Fatal("simple glob should match")
	}
	if globMatch("a.txt", "*.md") {
		t.Fatal("simple glob should not match wrong extension")
	}
	if !globMatch("docs/readme.md", "**/*.md") {
		t.Fatal("recursive glob should match nested file")
	}
	if !globMatch("src/app/main.go", "src/app/*.go") {
		t.Fatal("slash glob should match")
	}
	if globMatch("src/other.go", "src/app/*.go") {
		t.Fatal("slash glob should not match other dir")
	}
	if !strings.Contains(globRegexp("a?.?"), "[^/]") {
		t.Fatalf("globRegexp should include ? wildcard")
	}
	if p.IsBlocked(".env") == false {
		t.Fatal("secret must be blocked")
	}
	settings.WritableGlobs = []string{"*"}
	settings.DangerouslyAllowAllWrites = false
	p = New(settings)
	if p.writable("something.txt") {
		t.Fatal("catch-all without allow-all flag should block write")
	}
	settings.DangerouslyAllowAllWrites = true
	p = New(settings)
	if !p.writable("something.txt") {
		t.Fatal("fallback catch-all without allow-all flag should block write")
	}
}
