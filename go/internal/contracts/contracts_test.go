package contracts

import "testing"

func TestLoadCanonicalContract(t *testing.T) {
	document, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	if document.Server.Name != "chatrepo-mcp" || document.Server.Version != "0.2.0" {
		t.Fatalf("unexpected server metadata: %+v", document.Server)
	}
	if got, want := len(document.Tools), 87; got != want {
		t.Fatalf("tool count = %d, want %d", got, want)
	}
	for index, tool := range document.Tools {
		if tool.Name == "" || len(tool.InputSchema) == 0 {
			t.Fatalf("invalid tool at %d: %+v", index, tool)
		}
		if index > 0 && document.Tools[index-1].Name >= tool.Name {
			t.Fatalf("tools are not strictly sorted: %s then %s", document.Tools[index-1].Name, tool.Name)
		}
	}
}
