package app

import (
	"testing"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/contracts"
)

func BenchmarkLoadContracts(b *testing.B) {
	for i := 0; i < b.N; i++ {
		if _, err := contracts.Load(); err != nil {
			b.Fatal(err)
		}
	}
}

func BenchmarkServerCreation(b *testing.B) {
	root := b.TempDir()
	settings := appSettings(root)
	for i := 0; i < b.N; i++ {
		application, err := New(settings)
		if err != nil {
			b.Fatal(err)
		}
		if application == nil || application.Server == nil || len(application.Contract.Tools) == 0 {
			b.Fatal("application should be initialized")
		}
	}
}
