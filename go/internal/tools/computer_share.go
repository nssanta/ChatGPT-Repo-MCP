package tools

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

const (
	computerShareTTL      = 5 * time.Minute
	computerShareMaxFiles = 4
)

func computerShareDirectory() (string, error) {
	cache, err := os.UserCacheDir()
	if err != nil || strings.TrimSpace(cache) == "" {
		return "", fmt.Errorf("resolve user cache directory: %w", err)
	}
	return filepath.Join(cache, "chatrepo-mcp", "shared-screens"), nil
}

func (e *Engine) materializeComputerShare(result map[string]any) map[string]any {
	encoded, _ := result["image_b64"].(string)
	if encoded == "" {
		return failure("share_invalid", "computer share returned no image")
	}
	data, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return withError("share_invalid", err)
	}
	if int64(len(data)) > e.settings.FileTransferExportMaxBytes {
		return failure(
			"payload_too_large",
			fmt.Sprintf(
				"computer snapshot exceeds FILE_TRANSFER_EXPORT_MAX_BYTES (%d > %d)",
				len(data), e.settings.FileTransferExportMaxBytes,
			),
		)
	}
	mimeType, _ := result["mime_type"].(string)
	if mimeType == "" {
		mimeType = "image/png"
	}
	if mimeType != "image/png" {
		return failure("share_invalid", "computer snapshot must be image/png")
	}

	dir, err := computerShareDirectory()
	if err != nil {
		return withError("share_cache_failed", err)
	}
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return withError("share_cache_failed", err)
	}
	_ = os.Chmod(dir, 0o700)
	_ = cleanupComputerShareDirectory(dir, time.Now())

	snapshotID := strings.TrimSpace(stringArg(result, "snapshot_id", ""))
	shortID := snapshotID
	if len(shortID) > 8 {
		shortID = shortID[:8]
	}
	if shortID == "" {
		shortID = "snapshot"
	}
	file, err := os.CreateTemp(dir, "computer-snapshot-"+shortID+"-*.png")
	if err != nil {
		return withError("share_cache_failed", err)
	}
	path := file.Name()
	ok := false
	defer func() {
		if !ok {
			_ = os.Remove(path)
		}
	}()
	if err := file.Chmod(0o600); err != nil {
		_ = file.Close()
		return withError("share_cache_failed", err)
	}
	if _, err := file.Write(data); err != nil {
		_ = file.Close()
		return withError("share_cache_failed", err)
	}
	if err := file.Close(); err != nil {
		return withError("share_cache_failed", err)
	}

	sum := sha256.Sum256(data)
	token := base64.RawURLEncoding.EncodeToString([]byte(path))
	expiresAt := time.Now().UTC().Add(computerShareTTL)
	ok = true

	time.AfterFunc(computerShareTTL, func() {
		_ = os.Remove(path)
	})

	result["path"] = path
	result["name"] = filepath.Base(path)
	result["mime_type"] = mimeType
	result["size_bytes"] = len(data)
	result["sha256"] = hex.EncodeToString(sum[:])
	result["resource_uri"] = exportedFileResourcePrefix + token
	result["expires_at"] = expiresAt.Format(time.RFC3339Nano)
	return result
}

func cleanupComputerShareDirectory(dir string, now time.Time) int {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return 0
	}
	type candidate struct {
		path string
		mod  time.Time
	}
	files := make([]candidate, 0, len(entries))
	removed := 0
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), "computer-snapshot-") || !strings.HasSuffix(entry.Name(), ".png") {
			continue
		}
		full := filepath.Join(dir, entry.Name())
		info, err := entry.Info()
		if err != nil || !info.Mode().IsRegular() {
			continue
		}
		if now.Sub(info.ModTime()) >= computerShareTTL {
			if err := os.Remove(full); err == nil || os.IsNotExist(err) {
				removed++
			}
			continue
		}
		files = append(files, candidate{path: full, mod: info.ModTime()})
	}
	sort.Slice(files, func(i, j int) bool { return files[i].mod.Before(files[j].mod) })
	for len(files) >= computerShareMaxFiles {
		if err := os.Remove(files[0].path); err == nil || os.IsNotExist(err) {
			removed++
		}
		files = files[1:]
	}
	return removed
}

func isManagedComputerShare(path string) bool {
	dir, err := computerShareDirectory()
	if err != nil {
		return false
	}
	clean := filepath.Clean(path)
	if filepath.Dir(clean) != filepath.Clean(dir) {
		return false
	}
	base := filepath.Base(clean)
	return strings.HasPrefix(base, "computer-snapshot-") && strings.HasSuffix(base, ".png")
}
