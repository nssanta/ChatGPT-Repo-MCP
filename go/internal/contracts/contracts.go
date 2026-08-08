// Package contracts loads the generated copy of the repository-wide MCP
// contract. The JSON file is generated from ../../contracts/tool-schemas and
// must never be edited by hand.
package contracts

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"strings"
)

//go:embed tools.json
var raw []byte

// Document is the shared, language-neutral MCP contract.
type Document struct {
	ContractVersion int                        `json:"contractVersion"`
	Server          ServerInfo                 `json:"server"`
	ResultSchemas   map[string]json.RawMessage `json:"resultSchemas"`
	Tools           []Tool                     `json:"tools"`
}

// ServerInfo contains release metadata covered by the contract.
type ServerInfo struct {
	Name      string `json:"name"`
	Version   string `json:"version"`
	ToolCount int    `json:"toolCount"`
}

// Tool is the transport-neutral subset needed to register an MCP tool.
type Tool struct {
	Name         string          `json:"name"`
	Description  string          `json:"description"`
	InputSchema  json.RawMessage `json:"inputSchema"`
	OutputSchema json.RawMessage `json:"outputSchema"`
	Annotations  Annotations     `json:"annotations"`
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
	if document.ContractVersion != 3 {
		return Document{}, fmt.Errorf("unsupported tool contract version %d", document.ContractVersion)
	}
	if len(document.Tools) != 98 {
		return Document{}, fmt.Errorf("tool contract must contain exactly 98 tools, got %d", len(document.Tools))
	}
	for _, name := range []string{"boundedOutputReceiptV1", "artifactContinuationV1", "readArtifactResultV1"} {
		if len(document.ResultSchemas[name]) == 0 {
			return Document{}, fmt.Errorf("tool contract is missing result schema %q", name)
		}
	}
	seen := make(map[string]struct{}, len(document.Tools))
	for _, tool := range document.Tools {
		if tool.Name == "" {
			return Document{}, fmt.Errorf("tool contract contains an empty name")
		}
		if _, exists := seen[tool.Name]; exists {
			return Document{}, fmt.Errorf("tool contract contains duplicate %q", tool.Name)
		}
		if err := validateOutputSchema(tool.Name, tool.OutputSchema); err != nil {
			return Document{}, err
		}
		seen[tool.Name] = struct{}{}
	}
	return document, nil
}

func validateOutputSchema(name string, raw json.RawMessage) error {
	var schema struct {
		Type                 string                     `json:"type"`
		AdditionalProperties *bool                      `json:"additionalProperties"`
		Properties           map[string]json.RawMessage `json:"properties"`
	}
	if len(raw) == 0 {
		return fmt.Errorf("tool contract %q is missing outputSchema", name)
	}
	if err := json.Unmarshal(raw, &schema); err != nil {
		return fmt.Errorf("parse outputSchema for %q: %w", name, err)
	}
	if schema.Type != "object" {
		return fmt.Errorf("tool contract %q outputSchema must have object type", name)
	}
	if len(schema.Properties) == 0 {
		return fmt.Errorf("tool contract %q has a generic object-only outputSchema", name)
	}
	if schema.AdditionalProperties == nil || !*schema.AdditionalProperties {
		return fmt.Errorf("tool contract %q outputSchema must be additive", name)
	}
	for _, field := range []string{"ok", "error", "error_kind"} {
		if _, exists := schema.Properties[field]; !exists {
			return fmt.Errorf("tool contract %q outputSchema lacks typed error field %q", name, field)
		}
	}
	if len(schema.Properties) == 3 {
		return fmt.Errorf("tool contract %q outputSchema lacks success-specific fields", name)
	}
	var document map[string]any
	if err := json.Unmarshal(raw, &document); err != nil {
		return fmt.Errorf("parse outputSchema requirements for %q: %w", name, err)
	}
	definitions := schemaDefinitions(document)
	required := requiredBranches(document, definitions, make(map[string]bool))
	if len(required) == 0 || !rejectsEmptyObject(document, definitions, make(map[string]bool)) {
		return fmt.Errorf("tool contract %q outputSchema accepts an unconstrained empty object", name)
	}
	hasTypedError, hasLegacyError, hasSuccess := false, false, false
	for _, branch := range required {
		if branch["ok"] && branch["error_kind"] {
			hasTypedError = true
		}
		if branch["error"] {
			hasLegacyError = true
		}
		for field := range branch {
			if field != "ok" && field != "error" && field != "error_kind" {
				hasSuccess = true
			}
		}
	}
	if !hasTypedError || !hasLegacyError {
		return fmt.Errorf("tool contract %q outputSchema lacks its required error variants", name)
	}
	if !hasSuccess {
		return fmt.Errorf("tool contract %q outputSchema lacks required success fields", name)
	}
	if !variantBranchesAreAdditive(document, definitions) {
		return fmt.Errorf("tool contract %q outputSchema has a non-additive result variant", name)
	}
	return nil
}

func variantBranchesAreAdditive(schema map[string]any, definitions map[string]any) bool {
	var alternatives []any
	for _, keyword := range []string{"anyOf", "oneOf"} {
		if values, ok := schema[keyword].([]any); ok && len(values) > 0 {
			alternatives = values
			break
		}
	}
	if len(alternatives) == 0 {
		return false
	}
	for _, alternative := range alternatives {
		branch, ok := alternative.(map[string]any)
		if !ok || !additiveObjectBranch(branch, definitions, make(map[string]bool)) {
			return false
		}
	}
	return true
}

func additiveObjectBranch(node map[string]any, definitions map[string]any, seen map[string]bool) bool {
	if reference, ok := node["$ref"].(string); ok && strings.HasPrefix(reference, "#/$defs/") {
		name := strings.TrimPrefix(reference, "#/$defs/")
		if seen[name] {
			return false
		}
		definition, ok := definitions[name].(map[string]any)
		if !ok {
			return false
		}
		seen[name] = true
		return additiveObjectBranch(definition, definitions, seen)
	}
	typeName, _ := node["type"].(string)
	additional, _ := node["additionalProperties"].(bool)
	return typeName == "object" && additional
}

func rejectsEmptyObject(node map[string]any, definitions map[string]any, seen map[string]bool) bool {
	if fields, ok := node["required"].([]any); ok && len(fields) > 0 {
		return true
	}
	if reference, ok := node["$ref"].(string); ok && strings.HasPrefix(reference, "#/$defs/") {
		name := strings.TrimPrefix(reference, "#/$defs/")
		if seen[name] {
			return false
		}
		definition, ok := definitions[name].(map[string]any)
		if !ok {
			return false
		}
		nextSeen := make(map[string]bool, len(seen)+1)
		for key, value := range seen {
			nextSeen[key] = value
		}
		nextSeen[name] = true
		return rejectsEmptyObject(definition, definitions, nextSeen)
	}
	if branches, ok := node["allOf"].([]any); ok {
		for _, branch := range branches {
			if child, ok := branch.(map[string]any); ok && rejectsEmptyObject(child, definitions, seen) {
				return true
			}
		}
	}
	for _, keyword := range []string{"anyOf", "oneOf"} {
		branches, ok := node[keyword].([]any)
		if !ok || len(branches) == 0 {
			continue
		}
		allConstrained := true
		for _, branch := range branches {
			child, ok := branch.(map[string]any)
			if !ok || !rejectsEmptyObject(child, definitions, seen) {
				allConstrained = false
				break
			}
		}
		if allConstrained {
			return true
		}
	}
	return false
}

func schemaDefinitions(schema map[string]any) map[string]any {
	definitions, _ := schema["$defs"].(map[string]any)
	return definitions
}

func requiredBranches(node map[string]any, definitions map[string]any, seen map[string]bool) []map[string]bool {
	return requiredBranchesWithInherited(node, definitions, seen, nil)
}

func requiredBranchesWithInherited(node map[string]any, definitions map[string]any, seen map[string]bool, inherited map[string]bool) []map[string]bool {
	local := make(map[string]bool, len(inherited)+3)
	for field := range inherited {
		local[field] = true
	}
	if fields, ok := node["required"].([]any); ok {
		for _, field := range fields {
			if value, ok := field.(string); ok {
				local[value] = true
			}
		}
	}
	if reference, ok := node["$ref"].(string); ok && strings.HasPrefix(reference, "#/$defs/") {
		name := strings.TrimPrefix(reference, "#/$defs/")
		if seen[name] {
			return nil
		}
		definition, ok := definitions[name].(map[string]any)
		if !ok {
			return nil
		}
		nextSeen := make(map[string]bool, len(seen)+1)
		for key, value := range seen {
			nextSeen[key] = value
		}
		nextSeen[name] = true
		return requiredBranchesWithInherited(definition, definitions, nextSeen, local)
	}
	branches := make([]map[string]bool, 0)
	descended := false
	for _, keyword := range []string{"anyOf", "oneOf", "allOf"} {
		alternatives, _ := node[keyword].([]any)
		for _, alternative := range alternatives {
			if child, ok := alternative.(map[string]any); ok {
				descended = true
				branches = append(branches, requiredBranchesWithInherited(child, definitions, seen, local)...)
			}
		}
	}
	if child, ok := node["then"].(map[string]any); ok {
		descended = true
		branches = append(branches, requiredBranchesWithInherited(child, definitions, seen, local)...)
	}
	if !descended && len(local) > 0 {
		branches = append(branches, local)
	}
	return branches
}
