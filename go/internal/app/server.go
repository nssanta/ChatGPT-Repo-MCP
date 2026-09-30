// Package app wires configuration, contracts, tools, authentication, and MCP transports.
package app

import (
	"context"
	"crypto/subtle"
	"encoding/base64"
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
		if !toolEnabled(contractTool.Name, settings) {
			continue
		}
		names = append(names, contractTool.Name)
	}
	engine := tools.New(settings, names)
	for _, contractTool := range document.Tools {
		if !toolEnabled(contractTool.Name, settings) {
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
			InputSchema:  json.RawMessage(definition.InputSchema),
			OutputSchema: json.RawMessage(definition.OutputSchema),
			Annotations:  annotation,
			Meta:         mcp.Meta(definition.Meta),
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
			isError := result["ok"] == false
			structured := result
			content := []mcp.Content{}
			if imageB64, ok := result["image_b64"].(string); ok && imageB64 != "" && !isError {
				image, decodeErr := base64.StdEncoding.DecodeString(imageB64)
				if decodeErr != nil {
					return nil, fmt.Errorf("decode %s screenshot: %w", definition.Name, decodeErr)
				}
				structured = cloneResult(result)
				delete(structured, "image_b64")
				mimeType, _ := structured["mime_type"].(string)
				if mimeType == "" {
					mimeType = "image/png"
				}
				snapshotID, _ := structured["snapshot_id"].(string)
				content = append(content,
					&mcp.TextContent{Text: fmt.Sprintf("Fresh computer scene %s; use this snapshot_id for coordinate actions.", snapshotID)},
					&mcp.ImageContent{Data: image, MIMEType: mimeType},
				)
			} else {
				encoded, err := json.Marshal(structured)
				if err != nil {
					return nil, fmt.Errorf("marshal %s result: %w", definition.Name, err)
				}
				content = append(content, &mcp.TextContent{Text: string(encoded)})
			}
			if definition.Name == "export_file_to_chat" && !isError {
				uri, _ := result["resource_uri"].(string)
				name, _ := result["name"].(string)
				mimeType, _ := result["mime_type"].(string)
				description := fmt.Sprintf("File exported from %v", result["path"])
				size := resultSize(result["size_bytes"])
				if uri != "" && name != "" {
					content = append(content, &mcp.ResourceLink{
						URI: uri, Name: name, MIMEType: mimeType,
						Description: description, Size: size,
					})
				}
			}
			if definition.Name == "computer_share_snapshot" && !isError {
				uri, _ := result["resource_uri"].(string)
				name, _ := result["name"].(string)
				mimeType, _ := result["mime_type"].(string)
				size := resultSize(result["size_bytes"])
				if uri != "" && name != "" {
					content = append(content, &mcp.ResourceLink{
						URI: uri, Name: name, MIMEType: mimeType,
						Description: "RAM-only Computer Use snapshot shared with the current chat.",
						Size:        size,
					})
				}
			}
			return &mcp.CallToolResult{
				Content:           content,
				StructuredContent: structured,
				IsError:           isError,
			}, nil
		})
	}
	server.AddResourceTemplate(
		&mcp.ResourceTemplate{
			Name:        "chatrepo-exported-file",
			Description: "Binary-safe file resource created by export_file_to_chat.",
			URITemplate: "chatrepo-file://local/{token}",
		},
		func(ctx context.Context, request *mcp.ReadResourceRequest) (*mcp.ReadResourceResult, error) {
			if request == nil || request.Params == nil || request.Params.URI == "" {
				return nil, mcp.ResourceNotFoundError("")
			}
			var data []byte
			var mimeType string
			var err error
			const screenPrefix = "chatrepo-" + "file://local/screen-"
			if strings.HasPrefix(request.Params.URI, screenPrefix) {
				data, mimeType, err = engine.ReadComputerShareResource(ctx, request.Params.URI)
			} else {
				data, mimeType, err = engine.ReadExportResource(request.Params.URI)
			}
			if err != nil {
				return nil, mcp.ResourceNotFoundError(request.Params.URI)
			}
			return &mcp.ReadResourceResult{
				Contents: []*mcp.ResourceContents{{
					URI: request.Params.URI, MIMEType: mimeType, Blob: data,
				}},
			}, nil
		},
	)

	return &Application{Settings: settings, Contract: document, Server: server, Engine: engine}, nil
}

func cloneResult(source map[string]any) map[string]any {
	copy := make(map[string]any, len(source))
	for key, value := range source {
		copy[key] = value
	}
	return copy
}

func toolEnabled(name string, settings config.Settings) bool {
	if isPTYTool(name) && !(settings.FullAccess() && settings.EnablePTY && runtime.GOOS != "windows") {
		return false
	}
	if !isComputerTool(name) {
		return true
	}
	if !settings.ComputerUseEnabled {
		return false
	}
	if isComputerControlTool(name) && (!settings.ComputerControlEnabled || !settings.FullAccess()) {
		return false
	}
	return true
}

func isComputerTool(name string) bool {
	return strings.HasPrefix(name, "computer_")
}

func isComputerControlTool(name string) bool {
	switch name {
	case "computer_element", "computer_click", "computer_move", "computer_type", "computer_key", "computer_scroll",
		"computer_drag", "computer_window", "computer_launch", "computer_sequence":
		return true
	default:
		return false
	}
}

func resultSize(value any) *int64 {
	var size int64
	switch typed := value.(type) {
	case int64:
		size = typed
	case int:
		size = int64(typed)
	case float64:
		size = int64(typed)
	case json.Number:
		parsed, err := typed.Int64()
		if err != nil {
			return nil
		}
		size = parsed
	default:
		return nil
	}
	return &size
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
