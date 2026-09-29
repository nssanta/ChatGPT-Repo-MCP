package app

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
)

func appSettings(root string) config.Settings {
	return config.Settings{
		ProjectRoot: root, Transport: "streamable-http", Host: "127.0.0.1", Port: 0,
		AccessMode: "safe", BlockedGlobs: []string{".env", "**/.git/**"},
		SecretGlobs: []string{".env", "**/.git/**"}, WritableGlobs: []string{"**/*"},
		DangerouslyAllowAllWrites: true, MaxFileBytes: 1_000_000, MaxResponseChars: 100_000,
		MaxReadFiles: 25, MaxSearchResults: 100, MaxTreeEntries: 1000, MaxDiffBytes: 100_000,
		MaxLogCommits: 100, MaxWriteFileBytes: 1_000_000,
		FileTransferImportMaxBytes: 1_000_000, FileTransferExportMaxBytes: 1_000_000,
		MaxBatchOperations:   50,
		MaxCombinedDiffChars: 100_000, MaxPatchBytes: 100_000, MaxCommandOutputChars: 100_000,
		CommandTimeout: time.Second, CommandJobTimeout: time.Hour, SubprocessTimeout: time.Second, GitNetworkTimeout: time.Second,
		GHTimeout: time.Second, CommandJobsDir: tTemp(root, "jobs"), CommandAuditLogPath: tTemp(root, "audit.log"),
		CommandPolicyMode: "allowlist", AllowedHosts: []string{"localhost", "127.0.0.1"},
	}
}

func tTemp(root, name string) string { return root + "/" + name }

func TestServerListsCanonicalToolsAndCallsOne(t *testing.T) {
	application, err := New(appSettings(t.TempDir()))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	if _, err := application.Server.Connect(ctx, serverTransport, nil); err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()
	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(listed.Tools) != 94 {
		t.Fatalf("tools = %d", len(listed.Tools))
	}
	result, err := session.CallTool(ctx, &mcp.CallToolParams{Name: "list_repos", Arguments: map[string]any{}})
	if err != nil || result.IsError {
		t.Fatalf("call: result=%#v err=%v", result, err)
	}
}

func TestSecurityMiddleware(t *testing.T) {
	settings := appSettings(t.TempDir())
	settings.EnableDNSRebindingProtection = true
	settings.MCPAuthMode = "bearer"
	settings.MCPBearerToken = "secret"
	application, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}
	next := http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) { writer.WriteHeader(http.StatusNoContent) })
	handler := application.securityMiddleware(next)

	request := httptest.NewRequest(http.MethodPost, "http://evil.example/mcp", nil)
	response := httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusForbidden {
		t.Fatalf("host status = %d", response.Code)
	}
	request = httptest.NewRequest(http.MethodPost, "http://localhost/mcp", nil)
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusUnauthorized {
		t.Fatalf("auth status = %d", response.Code)
	}
	request = httptest.NewRequest(http.MethodPost, "http://localhost/mcp", nil)
	request.Header.Set("Authorization", "Bearer secret")
	response = httptest.NewRecorder()
	handler.ServeHTTP(response, request)
	if response.Code != http.StatusNoContent {
		t.Fatalf("authorized status = %d", response.Code)
	}
}

func TestFileTransferToolMetadataAndResourceRoundTrip(t *testing.T) {
	root := t.TempDir()
	payload := []byte{0x00, 0x01, 0x02, 0xff, 'x'}
	if err := os.WriteFile(tTemp(root, "artifact.bin"), payload, 0o644); err != nil {
		t.Fatal(err)
	}

	application, err := New(appSettings(root))
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	serverTransport, clientTransport := mcp.NewInMemoryTransports()
	if _, err := application.Server.Connect(ctx, serverTransport, nil); err != nil {
		t.Fatal(err)
	}
	client := mcp.NewClient(&mcp.Implementation{Name: "test", Version: "1"}, nil)
	session, err := client.Connect(ctx, clientTransport, nil)
	if err != nil {
		t.Fatal(err)
	}
	defer session.Close()

	listed, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	var receive *mcp.Tool
	for _, tool := range listed.Tools {
		if tool.Name == "receive_chat_file" {
			receive = tool
			break
		}
	}
	if receive == nil {
		t.Fatal("receive_chat_file missing")
	}
	params, ok := receive.Meta["openai/fileParams"].([]any)
	if !ok || len(params) != 1 || params[0] != "file" {
		t.Fatalf("file params metadata = %#v", receive.Meta)
	}

	result, err := session.CallTool(ctx, &mcp.CallToolParams{
		Name:      "export_file_to_chat",
		Arguments: map[string]any{"path": "artifact.bin"},
	})
	if err != nil || result.IsError {
		t.Fatalf("export: result=%#v err=%v", result, err)
	}
	var link *mcp.ResourceLink
	for _, item := range result.Content {
		if typed, ok := item.(*mcp.ResourceLink); ok {
			link = typed
			break
		}
	}
	if link == nil {
		t.Fatalf("export result lacks resource link: %#v", result.Content)
	}

	resource, err := session.ReadResource(ctx, &mcp.ReadResourceParams{URI: link.URI})
	if err != nil {
		t.Fatal(err)
	}
	if len(resource.Contents) != 1 || string(resource.Contents[0].Blob) != string(payload) {
		t.Fatalf("resource = %#v", resource.Contents)
	}
}
