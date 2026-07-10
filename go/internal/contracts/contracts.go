// Package contracts loads the generated copy of the repository-wide MCP
// contract. The JSON file is generated from ../../contracts/tool-schemas and
// must never be edited by hand.
package contracts

import (
	_ "embed"
	"encoding/json"
	"fmt"
)

//go:embed tools.json
var raw []byte

// Document is the shared, language-neutral MCP contract.
type Document struct {
	ContractVersion int        `json:"contractVersion"`
	Server          ServerInfo `json:"server"`
	Tools           []Tool     `json:"tools"`
}

// ServerInfo contains release metadata covered by the contract.
type ServerInfo struct {
	Name      string `json:"name"`
	Version   string `json:"version"`
	ToolCount int    `json:"toolCount"`
}

// Tool is the transport-neutral subset needed to register an MCP tool.
type Tool struct {
	Name        string          `json:"name"`
	Description string          `json:"description"`
	InputSchema json.RawMessage `json:"inputSchema"`
	Annotations Annotations     `json:"annotations"`
}

// Annotations mirrors MCP tool behavior hints. Pointer booleans retain an
// explicit false value from the canonical contract.
type Annotations struct {
	Title           string `json:"title"`
	ReadOnlyHint    *bool  `json:"readOnlyHint"`
	DestructiveHint *bool  `json:"destructiveHint"`
	OpenWorldHint   *bool  `json:"openWorldHint"`
}

// Load parses and validates the embedded contract.
func Load() (Document, error) {
	var document Document
	if err := json.Unmarshal(raw, &document); err != nil {
		return Document{}, fmt.Errorf("parse embedded tool contract: %w", err)
	}
	if document.Server.ToolCount != len(document.Tools) {
		return Document{}, fmt.Errorf(
			"tool contract count mismatch: metadata=%d tools=%d",
			document.Server.ToolCount,
			len(document.Tools),
		)
	}
	seen := make(map[string]struct{}, len(document.Tools))
	for _, tool := range document.Tools {
		if tool.Name == "" {
			return Document{}, fmt.Errorf("tool contract contains an empty name")
		}
		if _, exists := seen[tool.Name]; exists {
			return Document{}, fmt.Errorf("tool contract contains duplicate %q", tool.Name)
		}
		seen[tool.Name] = struct{}{}
	}
	return document, nil
}
