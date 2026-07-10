package tools

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"testing"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/contracts"
)

func benchmarkEngine(b *testing.B) (*Engine, string) {
	b.Helper()
	root := b.TempDir()
	document, err := contracts.Load()
	if err != nil {
		b.Fatal(err)
	}
	names := make([]string, 0, len(document.Tools))
	for _, tool := range document.Tools {
		names = append(names, tool.Name)
	}
	return New(testSettings(root), names), root
}

func BenchmarkListDir(b *testing.B) {
	engine, root := benchmarkEngine(b)
	if err := os.MkdirAll(filepath.Join(root, "nested"), 0o755); err != nil {
		b.Fatal(err)
	}
	for i := 0; i < 100; i++ {
		file := filepath.Join(root, "nested", fmt.Sprintf("f%03d.txt", i))
		if err := os.WriteFile(file, []byte("line\n"), 0o644); err != nil {
			b.Fatal(err)
		}
	}
	for i := 0; i < b.N; i++ {
		result := engine.Execute(context.Background(), "list_dir", map[string]any{"path": ".", "include_hidden": false})
		if result["ok"] != true {
			b.Fatalf("list_dir failed: %#v", result)
		}
	}
}

func BenchmarkReadTextFile(b *testing.B) {
	engine, root := benchmarkEngine(b)
	path := filepath.Join(root, "readme.md")
	if err := os.WriteFile(path, []byte("alpha\nbeta\ngamma\n"), 0o644); err != nil {
		b.Fatal(err)
	}
	for i := 0; i < b.N; i++ {
		result := engine.Execute(context.Background(), "read_text_file", map[string]any{"path": "readme.md", "with_line_numbers": false})
		if result["ok"] != true {
			b.Fatalf("read_text_file failed: %#v", result)
		}
	}
}

func BenchmarkSearchText(b *testing.B) {
	engine, root := benchmarkEngine(b)
	if err := os.WriteFile(filepath.Join(root, "notes.txt"), []byte("alpha\nneedle\nomega\nneedle\n"), 0o644); err != nil {
		b.Fatal(err)
	}
	for i := 0; i < b.N; i++ {
		result := engine.Execute(context.Background(), "search_text", map[string]any{"query": "needle", "path": "notes.txt", "limit": 10})
		if result["ok"] != true {
			b.Fatalf("search_text failed: %#v", result)
		}
	}
}

func BenchmarkReplaceTextDryRun(b *testing.B) {
	engine, root := benchmarkEngine(b)
	file := filepath.Join(root, "source.txt")
	if err := os.WriteFile(file, []byte("alpha\nold\nbeta\n"), 0o644); err != nil {
		b.Fatal(err)
	}
	read := engine.Execute(context.Background(), "read_text_file", map[string]any{"path": "source.txt", "with_line_numbers": false})
	if read["ok"] != true {
		b.Fatalf("read_text_file failed: %#v", read)
	}
	hash, _ := read["sha256"].(string)
	b.ReportAllocs()
	for i := 0; i < b.N; i++ {
		result := engine.Execute(context.Background(), "replace_text_in_file", map[string]any{
			"path":            "source.txt",
			"find":            "old",
			"replace":         "new",
			"expected_sha256": hash,
			"dry_run":         true,
			"replace_all":     false,
		})
		if result["ok"] != true {
			b.Fatalf("replace_text_in_file failed: %#v", result)
		}
	}
}
