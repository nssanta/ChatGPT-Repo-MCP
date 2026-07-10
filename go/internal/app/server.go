// Package app wires configuration, contracts, tools, authentication, and MCP transports.
package app

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"os"
	"runtime"
	"strings"
	"time"

	"github.com/modelcontextprotocol/go-sdk/mcp"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/config"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/contracts"
	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/tools"
)

// Version is overridden by release builds using -ldflags.
var Version = "dev"

// Application owns the fully registered MCP server.
type Application struct {
	Settings config.Settings
	Contract contracts.Document
	Server   *mcp.Server
	Engine   *tools.Engine
}

// New validates configuration and registers every canonical tool.
func New(settings config.Settings) (*Application, error) {
	document, err := contracts.Load()
	if err != nil {
		return nil, err
	}
	version := Version
	if version == "" || version == "dev" {
		version = document.Server.Version
	}
	server := mcp.NewServer(&mcp.Implementation{Name: document.Server.Name + "-go", Version: version}, nil)
	names := make([]string, 0, len(document.Tools))
	for _, contractTool := range document.Tools {
		if isPTYTool(contractTool.Name) && !(settings.FullAccess() && settings.EnablePTY && runtime.GOOS != "windows") {
			continue
		}
		names = append(names, contractTool.Name)
	}
	engine := tools.New(settings, names)
	for _, contractTool := range document.Tools {
		if isPTYTool(contractTool.Name) && !(settings.FullAccess() && settings.EnablePTY && runtime.GOOS != "windows") {
			continue
		}
		definition := contractTool
		annotation := &mcp.ToolAnnotations{Title: definition.Annotations.Title}
		if definition.Annotations.ReadOnlyHint != nil {
			annotation.ReadOnlyHint = *definition.Annotations.ReadOnlyHint
		}
		annotation.DestructiveHint = definition.Annotations.DestructiveHint
		annotation.OpenWorldHint = definition.Annotations.OpenWorldHint
		server.AddTool(&mcp.Tool{
			Name: definition.Name, Description: definition.Description,
			InputSchema: json.RawMessage(definition.InputSchema), Annotations: annotation,
		}, func(ctx context.Context, request *mcp.CallToolRequest) (*mcp.CallToolResult, error) {
			arguments := make(map[string]any)
			if len(request.Params.Arguments) > 0 {
				decoder := json.NewDecoder(strings.NewReader(string(request.Params.Arguments)))
				decoder.UseNumber()
				if err := decoder.Decode(&arguments); err != nil {
					return &mcp.CallToolResult{
						Content: []mcp.Content{&mcp.TextContent{Text: fmt.Sprintf(`{"ok":false,"error_kind":"invalid_arguments","error":%q}`, err.Error())}},
						IsError: true,
					}, nil
				}
			}
			result := engine.Execute(ctx, definition.Name, arguments)
			encoded, err := json.Marshal(result)
			if err != nil {
				return nil, fmt.Errorf("marshal %s result: %w", definition.Name, err)
			}
			isError := result["ok"] == false
			return &mcp.CallToolResult{
				Content:           []mcp.Content{&mcp.TextContent{Text: string(encoded)}},
				StructuredContent: result,
				IsError:           isError,
			}, nil
		})
	}
	return &Application{Settings: settings, Contract: document, Server: server, Engine: engine}, nil
}

func isPTYTool(name string) bool {
	switch name {
	case "start_terminal_session", "read_terminal_session", "write_terminal_session", "resize_terminal_session", "close_terminal_session", "list_terminal_sessions":
		return true
	default:
		return false
	}
}

// Run serves either stdio or Streamable HTTP until ctx is cancelled.
func (a *Application) Run(ctx context.Context) error {
	defer a.Engine.Shutdown()
	if a.Settings.Transport == "stdio" {
		return a.Server.Run(ctx, &mcp.StdioTransport{})
	}
	return a.runHTTP(ctx)
}

func (a *Application) runHTTP(ctx context.Context) error {
	mcpHandler := mcp.NewStreamableHTTPHandler(
		func(*http.Request) *mcp.Server { return a.Server },
		&mcp.StreamableHTTPOptions{JSONResponse: true, Logger: slog.Default()},
	)
	mux := http.NewServeMux()
	mux.Handle("/mcp", a.securityMiddleware(mcpHandler))
	mux.HandleFunc("/healthz", func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]any{"ok": true, "implementation": "go"})
	})
	mux.HandleFunc("/readyz", func(writer http.ResponseWriter, _ *http.Request) {
		writeJSON(writer, http.StatusOK, map[string]any{"ok": true, "tools": len(a.Engine.ToolNames()), "catalog_tools": a.Contract.Server.ToolCount})
	})

	address := net.JoinHostPort(a.Settings.Host, fmt.Sprint(a.Settings.Port))
	server := &http.Server{
		Addr: address, Handler: mux, ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout: 30 * time.Second, WriteTimeout: 0, IdleTimeout: 2 * time.Minute,
	}
	listener, err := net.Listen("tcp", address)
	if err != nil {
		return err
	}
	slog.Info("chatrepo-mcp Go server listening", "address", address, "path", "/mcp")
	errorChannel := make(chan error, 1)
	go func() { errorChannel <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
		shutdownContext, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()
		return server.Shutdown(shutdownContext)
	case err := <-errorChannel:
		if err == http.ErrServerClosed {
			return nil
		}
		return err
	}
}

func (a *Application) securityMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		if a.Settings.EnableDNSRebindingProtection && !hostAllowed(request.Host, a.Settings.AllowedHosts) {
			writeJSON(writer, http.StatusForbidden, map[string]any{"ok": false, "error": "Host header is not allowed"})
			return
		}
		if a.Settings.MCPAuthMode == "bearer" {
			authorization := request.Header.Get("Authorization")
			provided := strings.TrimSpace(strings.TrimPrefix(authorization, "Bearer "))
			expected := a.Settings.MCPBearerToken
			if !strings.HasPrefix(authorization, "Bearer ") || len(provided) != len(expected) || subtle.ConstantTimeCompare([]byte(provided), []byte(expected)) != 1 {
				writer.Header().Set("WWW-Authenticate", `Bearer realm="chatrepo-mcp"`)
				writeJSON(writer, http.StatusUnauthorized, map[string]any{"ok": false, "error": "invalid bearer token"})
				return
			}
		}
		next.ServeHTTP(writer, request)
	})
}

func hostAllowed(hostPort string, allowed []string) bool {
	host := hostPort
	if parsed, _, err := net.SplitHostPort(hostPort); err == nil {
		host = parsed
	}
	host = strings.Trim(host, "[]")
	for _, candidate := range allowed {
		if strings.HasSuffix(candidate, ":*") {
			candidate = strings.TrimSuffix(candidate, ":*")
		}
		candidateHost := candidate
		if parsed, _, err := net.SplitHostPort(candidate); err == nil {
			candidateHost = parsed
		}
		if strings.EqualFold(strings.Trim(candidateHost, "[]"), host) {
			return true
		}
	}
	return false
}

func writeJSON(writer http.ResponseWriter, status int, value any) {
	writer.Header().Set("Content-Type", "application/json")
	writer.WriteHeader(status)
	_ = json.NewEncoder(writer).Encode(value)
}

func init() {
	// Keep release binaries deterministic and avoid accidental environment logging.
	slog.SetDefault(slog.New(slog.NewTextHandler(os.Stderr, nil)))
}
