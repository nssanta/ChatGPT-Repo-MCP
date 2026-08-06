package config

import (
	"os"
	"path/filepath"
	"strconv"
	"testing"
	"time"
)

func cleanEnvironment(t *testing.T, root string) {
	t.Helper()
	for _, name := range []string{
		"PROJECT_ROOT", "ACCESS_MODE", "MCP_AUTH_MODE", "MCP_BEARER_TOKEN",
		"COMMAND_POLICY_MODE", "SECRET_GLOBS", "ALLOW_SECRET_ACCESS",
		"CANONICAL_NAMESPACE", "TRANSPORT", "WORKSPACE_ROOTS",
		"ENABLE_PTY", "PERSIST_FULL_OUTPUT", "RESOURCE_PROFILE", "RESOURCE_BUFFER_BYTES", "MAX_HEAVY_OPERATIONS",
		"DEFAULT_INLINE_OUTPUT_BYTES", "MAX_RESPONSE_CHARS", "MAX_DIFF_BYTES", "MAX_COMMAND_OUTPUT_CHARS",
	} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	t.Setenv("PROJECT_ROOT", root)
}

func TestLoadRejectsDisablingFullOutputPersistence(t *testing.T) {
	makeRoot(t)
	t.Setenv("PERSIST_FULL_OUTPUT", "false")
	if _, err := Load(); err == nil {
		t.Fatal("PERSIST_FULL_OUTPUT=false was accepted")
	}
}

func TestLoadRejectsUnsafeInlineOutputLimit(t *testing.T) {
	makeRoot(t)
	for _, value := range []string{"0", "-1", "200001"} {
		t.Run(value, func(t *testing.T) {
			t.Setenv("DEFAULT_INLINE_OUTPUT_BYTES", value)
			if _, err := Load(); err == nil {
				t.Fatalf("DEFAULT_INLINE_OUTPUT_BYTES=%s was accepted", value)
			}
		})
	}
}

func makeRoot(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	cleanEnvironment(t, root)
	return root
}

func setEnvPairs(t *testing.T, pairs map[string]string) {
	t.Helper()
	for key, value := range pairs {
		t.Setenv(key, value)
	}
}

func TestLoadSafeAndFullModes(t *testing.T) {
	root := makeRoot(t)
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.FullAccess() || !settings.EffectiveDryRun(nil) || settings.ConfirmationGranted(false) {
		t.Fatalf("unexpected safe defaults: %+v", settings)
	}
	if !settings.EnablePTY {
		t.Fatal("PTY should default to enabled while remaining gated by access mode")
	}
	if settings.CanonicalNamespace != "/"+filepath.Base(root) {
		t.Fatalf("namespace = %q", settings.CanonicalNamespace)
	}
	t.Setenv("ACCESS_MODE", "full")
	settings, err = Load()
	if err != nil {
		t.Fatal(err)
	}
	if !settings.FullAccess() || settings.EffectiveDryRun(nil) || !settings.ConfirmationGranted(false) || settings.CommandPolicyMode != "unrestricted" {
		t.Fatalf("unexpected full defaults: %+v", settings)
	}
}

func TestLoadDefaultsAndNormalization(t *testing.T) {
	root := makeRoot(t)
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.Host != "127.0.0.1" {
		t.Fatalf("default host = %q", settings.Host)
	}
	if settings.Transport != "streamable-http" {
		t.Fatalf("default transport = %q", settings.Transport)
	}
	if settings.CanonicalNamespace != "/"+filepath.Base(root) {
		t.Fatalf("unexpected canonical namespace: %q", settings.CanonicalNamespace)
	}
	if settings.WorkspaceScanDepth != 2 {
		t.Fatalf("unexpected scan depth: %d", settings.WorkspaceScanDepth)
	}
	if settings.MaxResponseChars != 1_000_000 {
		t.Fatalf("unexpected max response chars: %d", settings.MaxResponseChars)
	}
	if settings.DefaultInlineOutputBytes != 64*1024 {
		t.Fatalf("unexpected default inline output bytes: %d", settings.DefaultInlineOutputBytes)
	}
}

func TestLoadParsesDotEnvFallbackAndOverrides(t *testing.T) {
	root := makeRoot(t)
	if err := os.Chdir(root); err != nil {
		t.Fatal(err)
	}
	envPath := filepath.Join(root, ".env")
	if err := os.WriteFile(envPath, []byte("MCP_AUTH_MODE=none\nCOMMAND_TIMEOUT_MS=123\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MCP_AUTH_MODE", "bearer")
	t.Setenv("MCP_BEARER_TOKEN", "explicit")
	t.Setenv("PROJECT_ROOT", root)
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.MCPAuthMode != "bearer" {
		t.Fatalf("environment variable did not override .env: %q", settings.MCPAuthMode)
	}
	if settings.CommandTimeout != 123*time.Millisecond {
		t.Fatalf("COMMAND_TIMEOUT_MS parse broken: %v", settings.CommandTimeout)
	}
}

func TestLoadBoolAndIntParsingFallback(t *testing.T) {
	root := makeRoot(t)
	t.Setenv("PROJECT_ROOT", root)
	t.Setenv("MCP_AUTH_MODE", "none")
	t.Setenv("ALLOW_FORCE_PUSH", "on")
	t.Setenv("MAX_FILE_BYTES", "n/a")
	t.Setenv("COMMAND_TIMEOUT_MS", "invalid")
	t.Setenv("COMMAND_POLICY_MODE", "guarded")

	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if !settings.AllowForcePush {
		t.Fatal("bool parse for on failed")
	}
	// intEnv fallbacks should preserve safe defaults on invalid integer values.
	if settings.MaxFileBytes != 5_000_000 {
		t.Fatalf("invalid int env did not fall back: %d", settings.MaxFileBytes)
	}
}

func TestLoadCsvAndWorkspaceRoots(t *testing.T) {
	root := makeRoot(t)
	extra := t.TempDir()
	t.Setenv("PROJECT_ROOT", root)
	t.Setenv("WORKSPACE_ROOTS", ","+extra+",  ")
	t.Setenv("SECRET_GLOBS", ".env,.env.local")
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if len(settings.WorkspaceRoots) != 1 || settings.WorkspaceRoots[0] != filepath.Clean(extra) {
		t.Fatalf("workspace roots = %#v", settings.WorkspaceRoots)
	}
	if len(settings.SecretGlobs) != 2 || settings.SecretGlobs[0] != ".env" || settings.SecretGlobs[1] != ".env.local" {
		t.Fatalf("secret globs = %#v", settings.SecretGlobs)
	}
}

func TestLoadPathHomeExpansion(t *testing.T) {
	root := makeRoot(t)
	t.Setenv("WORKSPACE_ROOTS", "")
	t.Setenv("COMMAND_AUDIT_LOG_PATH", "~/."+strconv.Itoa(os.Getpid())+".audit.log")
	t.Setenv("CANONICAL_NAMESPACE", "")
	t.Setenv("MCP_AUTH_MODE", "none")
	settings, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if settings.ProjectRoot != filepath.Clean(root) {
		t.Fatalf("project root changed: %q", settings.ProjectRoot)
	}
	if settings.CommandAuditLogPath[:1] != "/" {
		t.Fatalf("audit path not expanded: %q", settings.CommandAuditLogPath)
	}
}

func TestLoadValidation(t *testing.T) {
	root := makeRoot(t)
	_ = root
	t.Setenv("ACCESS_MODE", "invalid")
	if _, err := Load(); err == nil {
		t.Fatal("expected invalid access mode error")
	}
	t.Setenv("ACCESS_MODE", "safe")
	t.Setenv("MCP_AUTH_MODE", "bearer")
	if _, err := Load(); err == nil {
		t.Fatal("expected missing bearer token error")
	}
	t.Setenv("MCP_BEARER_TOKEN", "test-token")
	t.Setenv("TRANSPORT", "invalid")
	if _, err := Load(); err == nil {
		t.Fatal("expected invalid transport error")
	}
}
