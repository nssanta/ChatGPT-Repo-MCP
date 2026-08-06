package contracts

import (
	"encoding/json"
	"slices"
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
	if document.ContractVersion != 2 {
		t.Fatalf("contract version = %d, want 2", document.ContractVersion)
	}
	if got, want := len(document.Tools), 96; got != want {
		t.Fatalf("tool count = %d, want %d", got, want)
	}
	foundArtifact := false
	for index, tool := range document.Tools {
		if tool.Name == "" || len(tool.InputSchema) == 0 {
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
