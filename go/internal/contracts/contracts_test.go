package contracts

import (
	"encoding/json"
	"slices"
	"strings"
	"testing"
)

func TestLoadCanonicalContract(t *testing.T) {
	document, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if document.Server.Name != "chatrepo-mcp" || document.Server.Version != "0.3.0" {
		t.Fatalf("unexpected server metadata: %+v", document.Server)
	}
	if document.ContractVersion != 3 {
		t.Fatalf("contract version = %d, want 3", document.ContractVersion)
	}
	if got, want := len(document.Tools), 98; got != want {
		t.Fatalf("tool count = %d, want %d", got, want)
	}
	foundArtifact := false
	for index, tool := range document.Tools {
		if tool.Name == "" || len(tool.InputSchema) == 0 || len(tool.OutputSchema) == 0 {
			t.Fatalf("invalid tool at %d: %+v", index, tool)
		}
		if index > 0 && document.Tools[index-1].Name >= tool.Name {
			t.Fatalf("tools are not strictly sorted: %s then %s", document.Tools[index-1].Name, tool.Name)
		}
		if tool.Name == "read_artifact" {
			foundArtifact = true
		}
	}
	if !foundArtifact {
		t.Fatal("read_artifact is missing from embedded contract")
	}
	var receipt struct {
		Properties struct {
			Reason struct {
				Enum []string `json:"enum"`
			} `json:"reason"`
		} `json:"properties"`
	}
	if err := json.Unmarshal(document.ResultSchemas["boundedOutputReceiptV1"], &receipt); err != nil {
		t.Fatal(err)
	}
	if !slices.Contains(receipt.Properties.Reason.Enum, "source_active") {
		t.Fatalf("bounded receipt reason enum lacks source_active: %v", receipt.Properties.Reason.Enum)
	}
}

func TestValidateOutputSchemaRejectsGenericAndClosedSchemas(t *testing.T) {
	tests := []struct {
		name   string
		schema string
		want   string
	}{
		{name: "missing", want: "missing outputSchema"},
		{name: "non-object", schema: `{"type":"string"}`, want: "must have object type"},
		{name: "generic", schema: `{"type":"object","additionalProperties":true}`, want: "generic object-only"},
		{name: "closed", schema: `{"type":"object","properties":{"ok":{"type":"boolean"}},"additionalProperties":false}`, want: "must be additive"},
		{name: "missing error envelope", schema: `{"type":"object","properties":{"value":{"type":"string"}},"additionalProperties":true}`, want: "lacks typed error field"},
		{name: "missing success fields", schema: `{"type":"object","properties":{"ok":{"type":"boolean"},"error":{"type":"string"},"error_kind":{"type":"string"}},"additionalProperties":true}`, want: "lacks success-specific fields"},
		{name: "all optional", schema: `{"type":"object","properties":{"ok":{"type":"boolean"},"error":{"type":"string"},"error_kind":{"type":"string"},"value":{"type":"string"}},"additionalProperties":true}`, want: "unconstrained empty object"},
		{name: "permissive union branch", schema: `{"type":"object","properties":{"ok":{"type":"boolean"},"error":{"type":"string"},"error_kind":{"type":"string"},"value":{"type":"string"}},"additionalProperties":true,"anyOf":[{}, {"required":["value"]}, {"required":["ok","error","error_kind"]}]}`, want: "unconstrained empty object"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			err := validateOutputSchema("demo", json.RawMessage(test.schema))
			if err == nil || !strings.Contains(err.Error(), test.want) {
				t.Fatalf("validateOutputSchema() error = %v, want substring %q", err, test.want)
			}
		})
	}
}

func TestValidateOutputSchemaAcceptsConcreteAdditiveSchema(t *testing.T) {
	for name, schema := range map[string]string{
		"union refs": `{"type":"object","properties":{"ok":{"type":"boolean"},"error":{"type":"string"},"error_kind":{"type":"string"},"value":{"type":"string"}},"additionalProperties":true,"$defs":{"Success":{"type":"object","required":["value"],"additionalProperties":true},"Error":{"type":"object","required":["ok","error","error_kind"],"additionalProperties":true}},"anyOf":[{"$ref":"#/$defs/Success"},{"$ref":"#/$defs/Error"}]}`,
	} {
		t.Run(name, func(t *testing.T) {
			if err := validateOutputSchema("demo", json.RawMessage(schema)); err != nil {
				t.Fatal(err)
			}
		})
	}
}
