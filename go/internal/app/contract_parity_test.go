package app

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/contracts"
)

func TestMCPContractParityForAllTools(t *testing.T) {
	settings := appSettings(t.TempDir())
	settings.AccessMode = "full"
	settings.EnablePTY = true
	application, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}
	contract, err := contracts.Load()
	if err != nil {
		t.Fatal(err)
	}

	expected := make(map[string]contracts.Tool, len(contract.Tools))
	for _, tool := range contract.Tools {
		expected[tool.Name] = tool
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
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

	listResult, err := session.ListTools(ctx, nil)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := len(listResult.Tools), len(contract.Tools); got != want {
		t.Fatalf("tools count mismatch: got=%d want=%d", got, want)
	}

	actual := make(map[string]*mcp.Tool, len(listResult.Tools))
	for _, tool := range listResult.Tools {
		actual[tool.Name] = tool
	}

	for name, contractTool := range expected {
		listed, ok := actual[name]
		if !ok {
			t.Fatalf("tool %q missing in registered MCP tools", name)
		}
		if listed.Description != contractTool.Description {
			t.Fatalf("tool %q description mismatch", name)
		}
		gotTitle := ""
		if listed.Annotations != nil {
			gotTitle = listed.Annotations.Title
		}
		if gotTitle != contractTool.Annotations.Title {
			t.Fatalf("tool %q title mismatch: got=%q want=%q", name, gotTitle, contractTool.Annotations.Title)
		}

		expectedSchema := canonicalizedJSON(t, contractTool.InputSchema)
		actualSchema := canonicalizedJSON(t, listed.InputSchema)
		if string(expectedSchema) != string(actualSchema) {
			t.Fatalf("tool %q inputSchema mismatch", name)
		}
		expectedOutputSchema := canonicalizedJSON(t, contractTool.OutputSchema)
		actualOutputSchema := canonicalizedJSON(t, listed.OutputSchema)
		if string(expectedOutputSchema) != string(actualOutputSchema) {
			t.Fatalf("tool %q outputSchema mismatch", name)
		}
		wantReadOnly := false
		if contractTool.Annotations.ReadOnlyHint != nil {
			wantReadOnly = *contractTool.Annotations.ReadOnlyHint
		}
		if listed.Annotations == nil {
			if contractTool.Annotations.ReadOnlyHint != nil || contractTool.Annotations.DestructiveHint != nil || contractTool.Annotations.OpenWorldHint != nil {
				t.Fatalf("tool %q annotations missing in registration", name)
			}
		} else {
			if listed.Annotations.ReadOnlyHint != wantReadOnly {
				t.Fatalf("tool %q readOnly mismatch: got=%t want=%t", name, listed.Annotations.ReadOnlyHint, wantReadOnly)
			}
			if !boolPtrEqual(listed.Annotations.DestructiveHint, contractTool.Annotations.DestructiveHint) {
				t.Fatalf("tool %q destructiveHint mismatch", name)
			}
			if !boolPtrEqual(listed.Annotations.OpenWorldHint, contractTool.Annotations.OpenWorldHint) {
				t.Fatalf("tool %q openWorldHint mismatch", name)
			}
		}
	}
}

func TestStreamableHTTPHealthReadyAndAuthLifecycle(t *testing.T) {
	settings := appSettings(t.TempDir())
	settings.Transport = "streamable-http"
	settings.MCPAuthMode = "bearer"
	settings.MCPBearerToken = "secret"
	settings.EnableDNSRebindingProtection = true
	port, err := freeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	settings.Port = port

	application, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	serverDone := make(chan error, 1)
	go func() { serverDone <- application.runHTTP(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-serverDone:
			if err != nil && !errors.Is(err, context.Canceled) && err != http.ErrServerClosed {
				t.Fatalf("runHTTP returned unexpected error: %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("server did not stop after context cancel")
		}
	})

	baseURL := "http://127.0.0.1:" + strconv.Itoa(port)
	waitForHTTPReady(t, baseURL+"/healthz")

	client := &http.Client{}
	request(t, client, http.MethodGet, baseURL+"/healthz", "", nil, "")
	ready := requestJSON[any](t, client, http.MethodGet, baseURL+"/readyz", "", nil, "")
	if got, want := int(ready["tools"].(float64)), len(application.Engine.ToolNames()); got != want {
		t.Fatalf("ready tools mismatch: got=%d want=%d", got, want)
	}

	guard := application.securityMiddleware(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) { writer.WriteHeader(http.StatusNoContent) }))

	hostRequest := httptest.NewRequest(http.MethodPost, "http://evil.example/mcp", strings.NewReader("{}"))
	hostRequest.Header.Set("Content-Type", "application/json")
	hostRequest.Header.Set("Authorization", "Bearer secret")
	hostResponse := httptest.NewRecorder()
	guard.ServeHTTP(hostResponse, hostRequest)
	if hostResponse.Result().StatusCode != http.StatusForbidden {
		t.Fatalf("expected forbidden for bad host, got=%d", hostResponse.Result().StatusCode)
	}

	authRequest := httptest.NewRequest(http.MethodPost, baseURL+"/mcp", strings.NewReader("{}"))
	authRequest.Header.Set("Content-Type", "application/json")
	authResponse := httptest.NewRecorder()
	guard.ServeHTTP(authResponse, authRequest)
	if authResponse.Result().StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized, got=%d", authResponse.Result().StatusCode)
	}
	okRequest := httptest.NewRequest(http.MethodPost, baseURL+"/mcp", strings.NewReader("{}"))
	okRequest.Host = "127.0.0.1"
	okRequest.Header.Set("Content-Type", "application/json")
	okRequest.Header.Set("Authorization", "Bearer secret")
	okResponse := httptest.NewRecorder()
	guard.ServeHTTP(okResponse, okRequest)
	if okResponse.Result().StatusCode == http.StatusForbidden || okResponse.Result().StatusCode == http.StatusUnauthorized {
		t.Fatalf("expected middleware pass for valid host/token, got=%d", okResponse.Result().StatusCode)
	}

	cancel()
}

func TestRunHTTPRejectsHostAndMissingBearerInOrder(t *testing.T) {
	settings := appSettings(t.TempDir())
	settings.Transport = "streamable-http"
	settings.MCPAuthMode = "bearer"
	settings.MCPBearerToken = "secret"
	settings.EnableDNSRebindingProtection = true
	port, err := freeLocalPort()
	if err != nil {
		t.Fatal(err)
	}
	settings.Port = port
	application, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	serverDone := make(chan error, 1)
	go func() { serverDone <- application.runHTTP(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-serverDone:
			if err != nil && !errors.Is(err, context.Canceled) && err != http.ErrServerClosed {
				t.Fatalf("runHTTP returned unexpected error: %v", err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("server did not stop after context cancel")
		}
	})
	baseURL := "http://127.0.0.1:" + strconv.Itoa(port)
	waitForHTTPReady(t, baseURL+"/healthz")

	client := &http.Client{}

	guard := application.securityMiddleware(http.HandlerFunc(func(writer http.ResponseWriter, _ *http.Request) { writer.WriteHeader(http.StatusNoContent) }))
	missingBearer := httptest.NewRequest(http.MethodPost, baseURL+"/mcp", strings.NewReader("{}"))
	missingBearer.Header.Set("Content-Type", "application/json")
	missingBearerResponse := httptest.NewRecorder()
	guard.ServeHTTP(missingBearerResponse, missingBearer)
	if missingBearerResponse.Result().StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized before bearer: %d", missingBearerResponse.Result().StatusCode)
	}

	badBearer := httptest.NewRequest(http.MethodPost, baseURL+"/mcp", strings.NewReader("{}"))
	badBearer.Header.Set("Authorization", "Bearer bad")
	badBearer.Header.Set("Content-Type", "application/json")
	badBearerResponse := httptest.NewRecorder()
	guard.ServeHTTP(badBearerResponse, badBearer)
	if badBearerResponse.Result().StatusCode != http.StatusUnauthorized {
		t.Fatalf("expected unauthorized for bad bearer: %d", badBearerResponse.Result().StatusCode)
	}

	hostBlocked := httptest.NewRequest(http.MethodPost, "http://evil.example/mcp", strings.NewReader("{}"))
	hostBlocked.Header.Set("Authorization", "Bearer secret")
	hostBlocked.Header.Set("Content-Type", "application/json")
	hostBlockedResponse := httptest.NewRecorder()
	guard.ServeHTTP(hostBlockedResponse, hostBlocked)
	if hostBlockedResponse.Result().StatusCode != http.StatusForbidden {
		t.Fatalf("expected forbidden for host gate: %d", hostBlockedResponse.Result().StatusCode)
	}

	request(t, client, http.MethodGet, baseURL+"/healthz", "", nil, "").Body.Close()
	cancel()
}

func TestRunStdIOUsesMCPTransport(t *testing.T) {
	settings := appSettings(t.TempDir())
	settings.Transport = "stdio"
	application, err := New(settings)
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- application.Run(ctx) }()
	cancel()
	select {
	case err := <-done:
		if err != nil && err != context.Canceled {
			t.Fatalf("stdio run unexpected error: %v", err)
		}
	case <-time.After(2 * time.Second):
		t.Fatal("stdio run did not stop after context cancellation")
	}
}

func requestJSON[T any](t *testing.T, client *http.Client, method, endpoint, body string, headers map[string]string, token string) map[string]T {
	resp := request(t, client, method, endpoint, body, headers, token)
	defer resp.Body.Close()
	payload, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatal(err)
	}
	if strings.TrimSpace(string(payload)) == "" {
		return make(map[string]T)
	}
	var parsed map[string]T
	if err := json.Unmarshal(payload, &parsed); err != nil {
		t.Fatalf("decode JSON from %s: %v\n%s", endpoint, err, payload)
	}
	return parsed
}

func request(t *testing.T, client *http.Client, method, endpoint, body string, headers map[string]string, token string) *http.Response {
	t.Helper()
	req, err := http.NewRequestWithContext(context.Background(), method, endpoint, strings.NewReader(body))
	if err != nil {
		t.Fatal(err)
	}
	for key, value := range headers {
		req.Header.Set(key, value)
	}
	req.Header.Set("Content-Type", "application/json")
	if token != "" {
		req.Header.Set("Authorization", "Bearer "+token)
	}
	if headers == nil {
		req.Header = http.Header{}
	}
	resp, err := client.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func boolPtrEqual(actual *bool, expected *bool) bool {
	if actual == nil || expected == nil {
		return actual == expected
	}
	return *actual == *expected
}

func canonicalizedJSON(t *testing.T, value any) string {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatalf("encode json: %v", err)
	}
	var decoded any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatalf("decode json: %v", err)
	}
	result, err := json.Marshal(decoded)
	if err != nil {
		t.Fatalf("canonicalize json: %v", err)
	}
	return string(result)
}

func freeLocalPort() (int, error) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return 0, err
	}
	port := listener.Addr().(*net.TCPAddr).Port
	_ = listener.Close()
	return port, nil
}

func waitForHTTPReady(t *testing.T, url string) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		resp, err := http.Get(url)
		if err == nil {
			resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return
			}
		}
		os.Stdout.Write([]byte("."))
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timeout waiting for %q", url)
}
