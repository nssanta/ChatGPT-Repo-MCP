package tools

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"testing"
	"time"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
)

func testSettingsForTools(root string) config.Settings {
	return config.Settings{
		ProjectRoot: root, WorkspaceScanDepth: 3, MaxTreeEntries: 1000, AccessMode: "safe",
		BlockedGlobs: []string{".env", "**/.git/**", "**/*.bin"}, SecretGlobs: []string{".env", "**/.git/**"},
		BinaryGlobs: []string{"**/*.bin"}, WritableGlobs: []string{"**/*"}, DangerouslyAllowAllWrites: true,
		DefaultInlineOutputBytes: 64 * 1024, MaxCommandOutputChars: 100000, CommandTimeout: 5 * time.Second, SubprocessTimeout: 5 * time.Second,
		GitNetworkTimeout: 5 * time.Second, CommandJobsDir: filepath.Join(root, ".jobs"), CommandAuditLogPath: filepath.Join(root, "audit.log"),
		CommandPolicyMode: "allowlist", CanonicalNamespace: "/",
	}
}

func TestResolveDirectoryValidation(t *testing.T) {
	root := t.TempDir()
	settings := testSettingsForTools(root)
	engine := New(settings, []string{})
	if _, err := engine.resolveDirectory("does-not-exist", true); err == nil {
		t.Fatal("expected missing directory to fail")
	}
	if _, err := engine.resolveDirectory("go.mod", true); err == nil {
		t.Fatal("expected file to fail")
	}
	if _, err := engine.resolveDirectory(".", true); err != nil {
		t.Fatalf("project root should resolve: %v", err)
	}
}

func TestResolveRepoAndWorkspaceRoots(t *testing.T) {
	root := t.TempDir()
	if err := exec.Command("git", "-C", root, "init", "-b", "main").Run(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module root\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	settings := testSettingsForTools(root)
	engine := New(settings, []string{})
	repo, err := engine.resolveRepo(context.Background(), "")
	if err != nil {
		t.Fatalf("repo root resolution: %v", err)
	}
	if repo != filepath.Clean(root) {
		t.Fatalf("unexpected repo %q", repo)
	}

	outside := t.TempDir()
	extra := filepath.Join(outside, "extra")
	if err := os.MkdirAll(extra, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := exec.Command("git", "-C", extra, "init", "-b", "main").Run(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(extra, "nested.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	if _, err := engine.resolveRepo(context.Background(), extra); err == nil {
		t.Fatal("expected workspace root restriction to reject extra repo")
	}
	settings.WorkspaceRoots = []string{extra}
	engine = New(settings, []string{})
	repo, err = engine.resolveRepo(context.Background(), extra)
	if err != nil {
		t.Fatalf("expected workspace root to allow extra path: %v", err)
	}
	if repo != filepath.Clean(extra) {
		t.Fatalf("unexpected repo path: %q", repo)
	}
}

func TestDetectStackAndPresets(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "go.mod"), []byte("module demo\n\ngo 1.25\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "package.json"), []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(root, "dist"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, "Makefile"), []byte("test:\n\techo ok\nlint:\n\techo ok\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	settings := testSettingsForTools(root)
	engine := New(settings, []string{})
	stacks := detectStack(root)
	if len(stacks) != 3 {
		t.Fatalf("stacks = %#v", stacks)
	}
	presets := engine.presetsFor(root)
	if presets["test"] == nil {
		t.Fatal("preset test is required")
	}
	if presets["test"].(map[string]any)["command"] != "make test" {
		t.Fatalf("unexpected test preset: %#v", presets["test"])
	}
}

func TestWorkspaceEntriesAndRepoListing(t *testing.T) {
	root := t.TempDir()
	settings := testSettingsForTools(root)
	engine := New(settings, []string{})

	a := filepath.Join(root, "service-a")
	b := filepath.Join(root, "service-b")
	for _, dir := range []string{a, b} {
		if err := os.Mkdir(dir, 0o755); err != nil {
			t.Fatal(err)
		}
		if err := exec.Command("git", "-C", dir, "init", "-b", "main").Run(); err != nil {
			t.Fatal(err)
		}
		if _, err := os.Create(filepath.Join(dir, "go.mod")); err != nil {
			t.Fatal(err)
		}
	}
	entries := engine.workspaceEntries(context.Background())
	if len(entries) != 2 {
		t.Fatalf("workspace entries = %#v", entries)
	}
	for _, entry := range entries {
		if entry["is_git"] != true {
			t.Fatalf("expected git workspace entries: %#v", entry)
		}
	}

	settings.WorkspaceScanDepth = 1
	engine = New(settings, []string{})
	plain := filepath.Join(root, "plain")
	if err := os.Mkdir(plain, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(plain, "README.md"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	entries = engine.workspaceEntries(context.Background())
	if len(entries) < 2 {
		t.Fatalf("non-git fallback entries missing: %#v", entries)
	}
}

func TestRepoEntryCapturesBranchAndDirtyState(t *testing.T) {
	root := t.TempDir()
	settings := testSettingsForTools(root)
	engine := New(settings, []string{})
	repoRoot := filepath.Join(root, "repo")
	if err := os.Mkdir(repoRoot, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := exec.Command("git", "-C", repoRoot, "init", "-b", "main").Run(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoRoot, "README.md"), []byte("a\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := exec.Command("git", "-C", repoRoot, "add", "README.md").Run(); err != nil {
		t.Fatal(err)
	}
	if err := exec.Command("git", "-C", repoRoot, "-c", "user.email=test@example.com", "-c", "user.name=Test", "commit", "-m", "init").Run(); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoRoot, "new.txt"), []byte("x"), 0o644); err != nil {
		t.Fatal(err)
	}
	entry := engine.repoEntry(context.Background(), repoRoot, "repo", true)
	if entry["path"] != "repo" || entry["is_git"] != true {
		t.Fatalf("unexpected repo entry: %#v", entry)
	}
	if entry["branch"] != "main" {
		t.Fatalf("unexpected branch: %#v", entry["branch"])
	}
	if entry["dirty"] != true {
		t.Fatalf("expected dirty state due to untracked file")
	}
}
