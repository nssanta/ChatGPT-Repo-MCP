package tools

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"net/netip"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestFileTransferExportRoundTrip(t *testing.T) {
	engine, root := newTestEngine(t)
	payload := []byte{0x00, 0x01, 0x02, 0xff, 'x'}
	path := filepath.Join(root, "artifact.bin")
	if err := os.WriteFile(path, payload, 0o644); err != nil {
		t.Fatal(err)
	}

	result := engine.Execute(context.Background(), "export_file_to_chat", map[string]any{"path": "artifact.bin"})
	if result["ok"] != true {
		t.Fatalf("export: %#v", result)
	}
	if result["size_bytes"] != int64(len(payload)) {
		t.Fatalf("size_bytes = %#v", result["size_bytes"])
	}
	digest := sha256.Sum256(payload)
	if result["sha256"] != hex.EncodeToString(digest[:]) {
		t.Fatalf("sha256 = %#v", result["sha256"])
	}
	uri, _ := result["resource_uri"].(string)
	if !strings.HasPrefix(uri, exportedFileResourcePrefix) {
		t.Fatalf("resource_uri = %q", uri)
	}
	data, mimeType, err := engine.ReadExportResource(uri)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != string(payload) {
		t.Fatalf("resource payload = %v", data)
	}
	if mimeType != "application/octet-stream" {
		t.Fatalf("mime type = %q", mimeType)
	}

	normalRead := engine.Execute(context.Background(), "read_text_file", map[string]any{"path": "artifact.bin"})
	if normalRead["ok"] == true {
		t.Fatalf("normal text read must keep binary policy: %#v", normalRead)
	}
}

func TestFileTransferExportLimitsAndSecretPolicy(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.FileTransferExportMaxBytes = 3
	if err := os.WriteFile(filepath.Join(root, "large.bin"), []byte("1234"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(root, ".env"), []byte("TOKEN=secret"), 0o600); err != nil {
		t.Fatal(err)
	}

	large := engine.Execute(context.Background(), "export_file_to_chat", map[string]any{"path": "large.bin"})
	if large["error_kind"] != "payload_too_large" {
		t.Fatalf("large export: %#v", large)
	}
	secret := engine.Execute(context.Background(), "export_file_to_chat", map[string]any{"path": ".env"})
	if secret["ok"] == true {
		t.Fatalf("secret export must be blocked: %#v", secret)
	}
	if _, _, err := engine.ReadExportResource("https://example.com/file"); err == nil {
		t.Fatal("invalid resource URI must fail")
	}
}

func TestReceiveChatFileDefaultsToSafeDryRun(t *testing.T) {
	engine, root := newTestEngine(t)
	result := engine.Execute(context.Background(), "receive_chat_file", map[string]any{
		"file": map[string]any{
			"download_url": "https://files.example.invalid/file",
			"file_id":      "file_123",
			"file_name":    "payload.bin",
			"mime_type":    "application/octet-stream",
		},
		"destination_path": "incoming/payload.bin",
	})
	if result["ok"] != true || result["dry_run"] != true {
		t.Fatalf("dry run: %#v", result)
	}
	if _, err := os.Stat(filepath.Join(root, "incoming", "payload.bin")); !os.IsNotExist(err) {
		t.Fatalf("dry run created destination: %v", err)
	}
}

func TestTransferURLAndPublicIPValidation(t *testing.T) {
	for _, raw := range []string{
		"http://example.com/file",
		"https://user:pass@example.com/file",
		"https://example.com:8443/file",
	} {
		if _, err := validateTransferHTTPSURL(raw); err == nil {
			t.Fatalf("expected URL rejection: %s", raw)
		}
	}
	if _, err := validateTransferHTTPSURL("https://example.com/file"); err != nil {
		t.Fatalf("valid HTTPS URL rejected: %v", err)
	}

	for _, raw := range []string{"127.0.0.1", "10.0.0.1", "100.64.0.1", "192.0.2.1", "2001:db8::1"} {
		if isPublicTransferIP(netip.MustParseAddr(raw)) {
			t.Fatalf("non-public address accepted: %s", raw)
		}
	}
	if !isPublicTransferIP(netip.MustParseAddr("8.8.8.8")) {
		t.Fatal("public address rejected")
	}
}
