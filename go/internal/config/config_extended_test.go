package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestLoadDotEnvSupportsExportAndQuotedValues(t *testing.T) {
	root := makeRoot(t)
	if err := os.Chdir(root); err != nil {
		t.Fatal(err)
	}
	envPath := filepath.Join(root, ".env")
	if err := os.WriteFile(envPath, []byte("export DOTENV_VALUE=from_file\nQUOTED=\"with spaces\"\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("DOTENV_VALUE", "from_env")
	t.Setenv("MCP_AUTH_MODE", "none")
	if _, err := Load(); err != nil {
		t.Fatal(err)
	}
	if got := os.Getenv("DOTENV_VALUE"); got != "from_env" {
		t.Fatalf("existing env should not be overwritten: %q", got)
	}
	if got := os.Getenv("QUOTED"); got != "with spaces" {
		t.Fatalf("quoted env value should be unwrapped: %q", got)
	}
}
