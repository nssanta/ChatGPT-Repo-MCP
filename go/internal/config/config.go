// Package config owns environment loading and access-mode normalization.
package config

import (
	"bufio"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	defaultBlocked = ".env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**,**/.venv/**,**/node_modules/**,**/*.db,**/*.sqlite,**/*.sqlite3,**/*.bin,**/*.png,**/*.jpg,**/*.jpeg,**/*.webp,**/*.pdf,**/*.zip,**/*.tar,**/*.gz"
	defaultSecrets = ".env,.env.*,*.pem,*.key,*.p12,*.pfx,**/.git/**"
	defaultBinary  = "**/.venv/**,**/node_modules/**,**/*.db,**/*.sqlite,**/*.sqlite3,**/*.bin,**/*.png,**/*.jpg,**/*.jpeg,**/*.webp,**/*.pdf,**/*.zip,**/*.tar,**/*.gz"
)

// Settings is the normalized shared .env contract used by the Go server.
type Settings struct {
	AppName                      string
	Host                         string
	Port                         int
	Transport                    string
	ProjectRoot                  string
	WorkspaceRoots               []string
	FilesystemUnrestricted       bool
	WorkspaceScanDepth           int
	AccessMode                   string
	AllowSecretAccess            bool
	AllowHardReset               bool
	BlockedGlobs                 []string
	SecretGlobs                  []string
	BinaryGlobs                  []string
	WritableGlobs                []string
	DangerouslyAllowAllWrites    bool
	RequireExpectedHashForWrites bool
	AllowMoveDeleteOperations    bool
	AllowHiddenDefault           bool
	MaxFileBytes                 int64
	MaxResponseChars             int
	MaxReadFiles                 int
	MaxSearchResults             int
	MaxTreeEntries               int
	MaxDiffBytes                 int
	MaxLogCommits                int
	MaxWriteFileBytes            int64
	MaxBatchOperations           int
	MaxCombinedDiffChars         int
	MaxPatchBytes                int
	MaxCommandOutputChars        int
	CommandTimeout               time.Duration
	SubprocessTimeout            time.Duration
	GitNetworkTimeout            time.Duration
	GHTimeout                    time.Duration
	CommandAuditLogPath          string
	CommandJobsDir               string
	CommandPolicyMode            string
	DeniedWords                  []string
	DestructiveWords             []string
	CommandShellPrelude          string
	MCPExtraPath                 []string
	KillGrace                    time.Duration
	EnablePTY                    bool
	MaxTerminalSessions          int
	ProtectedBranches            []string
	AllowForcePush               bool
	GitHubToolsEnabled           bool
	MCPAuthMode                  string
	MCPBearerToken               string
	AllowedHosts                 []string
	EnableDNSRebindingProtection bool
	CanonicalNamespace           string
	EphemeralHandlesSupported    bool
}

// Load reads .env without overriding exported variables, then validates and
// normalizes the environment exactly once at process startup.
func Load() (Settings, error) {
	if err := loadDotEnv(".env"); err != nil && !errors.Is(err, os.ErrNotExist) {
		return Settings{}, fmt.Errorf("load .env: %w", err)
	}

	root := strings.TrimSpace(os.Getenv("PROJECT_ROOT"))
	if root == "" {
		return Settings{}, errors.New("PROJECT_ROOT is required")
	}
	root, err := filepath.Abs(expandHome(root))
	if err != nil {
		return Settings{}, fmt.Errorf("resolve PROJECT_ROOT: %w", err)
	}
	root, err = filepath.EvalSymlinks(root)
	if err != nil {
		return Settings{}, fmt.Errorf("PROJECT_ROOT must be an existing directory: %s", root)
	}
	info, err := os.Stat(root)
	if err != nil || !info.IsDir() {
		return Settings{}, fmt.Errorf("PROJECT_ROOT must be an existing directory: %s", root)
	}

	accessMode := lowerEnv("ACCESS_MODE", "safe")
	if accessMode != "safe" && accessMode != "full" {
		return Settings{}, errors.New("ACCESS_MODE must be one of: safe, full")
	}
	full := accessMode == "full"
	authMode := lowerEnv("MCP_AUTH_MODE", "none")
	if authMode != "none" && authMode != "bearer" {
		return Settings{}, errors.New("MCP_AUTH_MODE must be one of: none, bearer")
	}
	token := os.Getenv("MCP_BEARER_TOKEN")
	if authMode == "bearer" && strings.TrimSpace(token) == "" {
		return Settings{}, errors.New("MCP_BEARER_TOKEN is required when MCP_AUTH_MODE=bearer")
	}
	policy := lowerEnv("COMMAND_POLICY_MODE", "allowlist")
	if full {
		policy = "unrestricted"
	}
	if policy != "allowlist" && policy != "guarded" && policy != "unrestricted" && policy != "full_repo" {
		return Settings{}, errors.New("COMMAND_POLICY_MODE must be one of: allowlist, guarded, unrestricted, full_repo")
	}

	workspaceRoots := csvEnv("WORKSPACE_ROOTS", "")
	for index, path := range workspaceRoots {
		absolute, absErr := filepath.Abs(expandHome(path))
		if absErr != nil {
			return Settings{}, fmt.Errorf("resolve WORKSPACE_ROOTS entry %q: %w", path, absErr)
		}
		workspaceRoots[index] = filepath.Clean(absolute)
	}
	secretGlobs := csvEnv("SECRET_GLOBS", defaultSecrets)
	allowSecrets := full && boolEnv("ALLOW_SECRET_ACCESS", false)
	if len(secretGlobs) == 0 && !allowSecrets {
		return Settings{}, errors.New("SECRET_GLOBS must not be empty unless ACCESS_MODE=full and ALLOW_SECRET_ACCESS=true")
	}

	settings := Settings{
		AppName:                      stringEnv("APP_NAME", "chatrepo-mcp"),
		Host:                         stringEnv("HOST", "127.0.0.1"),
		Port:                         intEnv("PORT", 8000),
		Transport:                    lowerEnv("TRANSPORT", "streamable-http"),
		ProjectRoot:                  filepath.Clean(root),
		WorkspaceRoots:               workspaceRoots,
		FilesystemUnrestricted:       full || boolEnv("FILESYSTEM_UNRESTRICTED", false),
		WorkspaceScanDepth:           intEnv("WORKSPACE_SCAN_DEPTH", 2),
		AccessMode:                   accessMode,
		AllowSecretAccess:            allowSecrets,
		AllowHardReset:               boolEnv("ALLOW_HARD_RESET", false),
		BlockedGlobs:                 csvEnv("BLOCKED_GLOBS", defaultBlocked),
		SecretGlobs:                  secretGlobs,
		BinaryGlobs:                  csvEnv("BINARY_GLOBS", defaultBinary),
		WritableGlobs:                csvEnv("WRITABLE_GLOBS", "**/*"),
		DangerouslyAllowAllWrites:    full || boolEnv("DANGEROUSLY_ALLOW_ALL_WRITES", false),
		RequireExpectedHashForWrites: !full && boolEnv("REQUIRE_EXPECTED_HASH_FOR_WRITES", true),
		AllowMoveDeleteOperations:    full || boolEnv("ALLOW_MOVE_DELETE_OPERATIONS", false),
		AllowHiddenDefault:           boolEnv("ALLOW_HIDDEN_DEFAULT", true),
		MaxFileBytes:                 int64(intEnv("MAX_FILE_BYTES", 5_000_000)),
		MaxResponseChars:             intEnv("MAX_RESPONSE_CHARS", 1_000_000),
		MaxReadFiles:                 intEnv("MAX_READ_FILES", 25),
		MaxSearchResults:             intEnv("MAX_SEARCH_RESULTS", 500),
		MaxTreeEntries:               intEnv("MAX_TREE_ENTRIES", 5_000),
		MaxDiffBytes:                 intEnv("MAX_DIFF_BYTES", 1_000_000),
		MaxLogCommits:                intEnv("MAX_LOG_COMMITS", 100),
		MaxWriteFileBytes:            int64(intEnv("MAX_WRITE_FILE_BYTES", 1_000_000)),
		MaxBatchOperations:           intEnv("MAX_BATCH_OPERATIONS", 50),
		MaxCombinedDiffChars:         intEnv("MAX_COMBINED_DIFF_CHARS", 300_000),
		MaxPatchBytes:                intEnv("MAX_PATCH_BYTES", 500_000),
		MaxCommandOutputChars:        intEnv("MAX_COMMAND_OUTPUT_CHARS", 200_000),
		CommandTimeout:               time.Duration(intEnv("COMMAND_TIMEOUT_MS", 300_000)) * time.Millisecond,
		SubprocessTimeout:            time.Duration(intEnv("SUBPROCESS_TIMEOUT", 15)) * time.Second,
		GitNetworkTimeout:            time.Duration(intEnv("GIT_NETWORK_TIMEOUT", 60)) * time.Second,
		GHTimeout:                    time.Duration(intEnv("GH_TIMEOUT", 60)) * time.Second,
		CommandAuditLogPath:          expandHome(stringEnv("COMMAND_AUDIT_LOG_PATH", "~/.local/state/chatrepo-mcp/commands.log")),
		CommandJobsDir:               expandHome(stringEnv("COMMAND_JOBS_DIR", "/tmp/chatrepo-mcp-jobs")),
		CommandPolicyMode:            policy,
		DeniedWords:                  csvEnv("DENIED_WORDS", "sudo,su"),
		DestructiveWords:             csvEnv("DESTRUCTIVE_WORDS", "rm -rf,rmdir,git push --force,git reset --hard,git clean,docker system prune,chmod -R,chown -R,mkfs,dd"),
		CommandShellPrelude:          os.Getenv("COMMAND_SHELL_PRELUDE"),
		MCPExtraPath:                 pathListEnv("MCP_EXTRA_PATH"),
		KillGrace:                    time.Duration(intEnv("KILL_GRACE_MS", 5_000)) * time.Millisecond,
		EnablePTY:                    boolEnv("ENABLE_PTY", false),
		MaxTerminalSessions:          intEnv("MAX_TERMINAL_SESSIONS", 4),
		ProtectedBranches:            csvEnv("PROTECTED_BRANCHES", "main,master"),
		AllowForcePush:               boolEnv("ALLOW_FORCE_PUSH", false),
		GitHubToolsEnabled:           boolEnv("GITHUB_TOOLS_ENABLED", true),
		MCPAuthMode:                  authMode,
		MCPBearerToken:               token,
		AllowedHosts:                 csvEnv("ALLOWED_HOSTS", "127.0.0.1:*,localhost:*"),
		EnableDNSRebindingProtection: boolEnv("ENABLE_DNS_REBINDING_PROTECTION", true),
		CanonicalNamespace:           os.Getenv("CANONICAL_NAMESPACE"),
		EphemeralHandlesSupported:    boolEnv("EPHEMERAL_HANDLES_SUPPORTED", false),
	}
	if settings.CanonicalNamespace == "" {
		settings.CanonicalNamespace = "/" + filepath.Base(settings.ProjectRoot)
	}
	if settings.Transport != "streamable-http" && settings.Transport != "stdio" {
		return Settings{}, errors.New("TRANSPORT must be one of: streamable-http, stdio")
	}
	return settings, nil
}

func pathListEnv(name string) []string {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return nil
	}
	var values []string
	for _, value := range filepath.SplitList(raw) {
		if value = strings.TrimSpace(value); value != "" {
			values = append(values, value)
		}
	}
	return values
}

// FullAccess reports whether trusted-machine defaults are enabled.
func (s Settings) FullAccess() bool { return s.AccessMode == "full" }

// EffectiveDryRun applies ACCESS_MODE only when the caller omitted dry_run.
func (s Settings) EffectiveDryRun(requested *bool) bool {
	if requested != nil {
		return *requested
	}
	return !s.FullAccess()
}

// ConfirmationGranted removes internal confirmation gates only in full mode.
func (s Settings) ConfirmationGranted(confirmed bool) bool {
	return s.FullAccess() || confirmed
}

func loadDotEnv(path string) error {
	file, err := os.Open(path)
	if err != nil {
		return err
	}
	defer file.Close()
	scanner := bufio.NewScanner(file)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok || strings.TrimSpace(key) == "" {
			continue
		}
		key = strings.TrimSpace(key)
		if _, exists := os.LookupEnv(key); exists {
			continue
		}
		value = strings.TrimSpace(value)
		if len(value) >= 2 && ((value[0] == '\'' && value[len(value)-1] == '\'') || (value[0] == '"' && value[len(value)-1] == '"')) {
			value = value[1 : len(value)-1]
		}
		if err := os.Setenv(key, value); err != nil {
			return err
		}
	}
	return scanner.Err()
}

func stringEnv(name, fallback string) string {
	if value, ok := os.LookupEnv(name); ok {
		return value
	}
	return fallback
}

func lowerEnv(name, fallback string) string {
	return strings.ToLower(strings.TrimSpace(stringEnv(name, fallback)))
}

func boolEnv(name string, fallback bool) bool {
	value, ok := os.LookupEnv(name)
	if !ok {
		return fallback
	}
	switch strings.ToLower(strings.TrimSpace(value)) {
	case "1", "true", "yes", "y", "on":
		return true
	default:
		return false
	}
}

func intEnv(name string, fallback int) int {
	value := strings.TrimSpace(os.Getenv(name))
	if value == "" {
		return fallback
	}
	parsed, err := strconv.Atoi(value)
	if err != nil {
		return fallback
	}
	return parsed
}

func csvEnv(name, fallback string) []string {
	value, ok := os.LookupEnv(name)
	if !ok {
		value = fallback
	}
	var result []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			result = append(result, item)
		}
	}
	return result
}

func expandHome(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, strings.TrimPrefix(path, "~/"))
		}
	}
	return path
}
