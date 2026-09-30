package tools

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"mime"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"time"
)

const exportedFileResourcePrefix = "chatrepo-file://local/"

var nonPublicTransferPrefixes = []netip.Prefix{
	netip.MustParsePrefix("100.64.0.0/10"),
	netip.MustParsePrefix("192.0.0.0/24"),
	netip.MustParsePrefix("192.0.2.0/24"),
	netip.MustParsePrefix("192.88.99.0/24"),
	netip.MustParsePrefix("198.18.0.0/15"),
	netip.MustParsePrefix("198.51.100.0/24"),
	netip.MustParsePrefix("203.0.113.0/24"),
	netip.MustParsePrefix("2001:db8::/32"),
}

func (e *Engine) receiveChatFile(ctx context.Context, args map[string]any) map[string]any {
	destination := strings.TrimSpace(stringArg(args, "destination_path", ""))
	if destination == "" {
		return failure("invalid_path", "destination_path must not be empty")
	}
	file := mapArg(args, "file")
	if file == nil {
		return failure("invalid_file_param", "ChatGPT file parameter is required")
	}
	fileID := strings.TrimSpace(stringArg(file, "file_id", ""))
	downloadURL := strings.TrimSpace(stringArg(file, "download_url", ""))
	if fileID == "" || downloadURL == "" {
		return failure("invalid_file_param", "ChatGPT file parameter is missing file_id or download_url")
	}

	resolved, err := e.perimeter.ResolveTransfer(destination, e.settings.AllowHiddenDefault, true)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	if _, err := os.Lstat(resolved.Absolute); err == nil {
		return failure("destination_exists", fmt.Sprintf("destination already exists: %s", e.perimeter.Display(resolved.Absolute)))
	} else if !os.IsNotExist(err) {
		return withError("destination_stat_failed", err)
	}

	fileName := strings.TrimSpace(stringArg(file, "file_name", ""))
	if fileName == "" {
		fileName = filepath.Base(resolved.Absolute)
	}
	mimeType := strings.TrimSpace(stringArg(file, "mime_type", ""))
	if mimeType == "" {
		mimeType = mime.TypeByExtension(filepath.Ext(fileName))
	}
	if mimeType == "" {
		mimeType = "application/octet-stream"
	}

	dryRun := e.settings.EffectiveDryRun(optionalBool(args, "dry_run"))
	if dryRun {
		return map[string]any{
			"ok": true, "path": e.perimeter.Display(resolved.Absolute),
			"file_id": fileID, "file_name": fileName, "mime_type": mimeType,
			"size_bytes": nil, "sha256": nil, "dry_run": true,
		}
	}

	if err := os.MkdirAll(filepath.Dir(resolved.Absolute), 0o755); err != nil {
		return withError("create_parent_failed", err)
	}
	tempPath, size, digest, err := downloadTransferFile(
		ctx, downloadURL, filepath.Dir(resolved.Absolute), e.settings.FileTransferImportMaxBytes,
	)
	if err != nil {
		return withError("download_failed", err)
	}
	defer os.Remove(tempPath)

	if err := os.Link(tempPath, resolved.Absolute); err != nil {
		if _, statErr := os.Lstat(resolved.Absolute); statErr == nil {
			return failure("destination_exists", fmt.Sprintf("destination already exists: %s", e.perimeter.Display(resolved.Absolute)))
		}
		return withError("publish_failed", err)
	}

	return map[string]any{
		"ok": true, "path": e.perimeter.Display(resolved.Absolute),
		"file_id": fileID, "file_name": fileName, "mime_type": mimeType,
		"size_bytes": size, "sha256": digest, "dry_run": false,
	}
}

func (e *Engine) exportFileToChat(args map[string]any) map[string]any {
	path := strings.TrimSpace(stringArg(args, "path", ""))
	if path == "" {
		return failure("invalid_path", "path must not be empty")
	}
	resolved, err := e.perimeter.ResolveTransfer(path, e.settings.AllowHiddenDefault, false)
	if err != nil {
		return withError("path_not_allowed", err)
	}
	info, err := os.Stat(resolved.Absolute)
	if err != nil {
		if os.IsNotExist(err) {
			return failure("file_not_found", fmt.Sprintf("file does not exist: %s", e.perimeter.Display(resolved.Absolute)))
		}
		return withError("file_stat_failed", err)
	}
	if !info.Mode().IsRegular() {
		return failure("not_a_file", fmt.Sprintf("path is not a regular file: %s", e.perimeter.Display(resolved.Absolute)))
	}
	if info.Size() > e.settings.FileTransferExportMaxBytes {
		return failure(
			"payload_too_large",
			fmt.Sprintf("file exceeds FILE_TRANSFER_EXPORT_MAX_BYTES (%d > %d)", info.Size(), e.settings.FileTransferExportMaxBytes),
		)
	}

	size, digest, err := hashFileLimited(resolved.Absolute, e.settings.FileTransferExportMaxBytes)
	if err != nil {
		return withError("export_failed", err)
	}
	mimeType := mime.TypeByExtension(filepath.Ext(resolved.Absolute))
	if mimeType == "" {
		mimeType = "application/octet-stream"
	}
	token := base64.RawURLEncoding.EncodeToString([]byte(resolved.Absolute))
	return map[string]any{
		"ok": true, "path": e.perimeter.Display(resolved.Absolute),
		"name": filepath.Base(resolved.Absolute), "mime_type": mimeType,
		"size_bytes": size, "sha256": digest,
		"resource_uri": exportedFileResourcePrefix + token,
	}
}

// ReadExportResource resolves an export_file_to_chat ResourceLink and returns
// its bytes after rechecking the current path, secret policy, and size limit.
func (e *Engine) ReadExportResource(rawURI string) ([]byte, string, error) {
	parsed, err := url.Parse(rawURI)
	if err != nil || parsed.Scheme != "chatrepo-file" || parsed.Host != "local" || parsed.RawQuery != "" || parsed.Fragment != "" {
		return nil, "", fmt.Errorf("invalid exported-file resource URI")
	}
	token := strings.TrimPrefix(parsed.Path, "/")
	if token == "" || len(token) > 16384 {
		return nil, "", fmt.Errorf("invalid exported-file resource token")
	}
	decoded, err := base64.RawURLEncoding.DecodeString(token)
	if err != nil || len(decoded) == 0 || strings.IndexByte(string(decoded), 0) >= 0 {
		return nil, "", fmt.Errorf("invalid exported-file resource token")
	}
	decodedPath := string(decoded)
	absolute := ""
	if isManagedComputerShare(decodedPath) {
		absolute = filepath.Clean(decodedPath)
	} else {
		resolved, resolveErr := e.perimeter.ResolveTransfer(decodedPath, e.settings.AllowHiddenDefault, false)
		if resolveErr != nil {
			return nil, "", resolveErr
		}
		absolute = resolved.Absolute
	}
	info, err := os.Lstat(absolute)
	if err != nil {
		return nil, "", err
	}
	if info.Mode()&os.ModeSymlink != 0 || !info.Mode().IsRegular() {
		return nil, "", fmt.Errorf("exported path is not a regular file")
	}
	if isManagedComputerShare(absolute) && time.Since(info.ModTime()) >= computerShareTTL {
		_ = os.Remove(absolute)
		return nil, "", fmt.Errorf("shared computer snapshot expired")
	}
	data, err := readFileLimited(absolute, e.settings.FileTransferExportMaxBytes)
	if err != nil {
		return nil, "", err
	}
	mimeType := mime.TypeByExtension(filepath.Ext(absolute))
	if mimeType == "" {
		mimeType = "application/octet-stream"
	}
	return data, mimeType, nil
}

func downloadTransferFile(ctx context.Context, rawURL, directory string, maxBytes int64) (string, int64, string, error) {
	if _, err := validateTransferHTTPSURL(rawURL); err != nil {
		return "", 0, "", err
	}
	client := safeTransferHTTPClient()
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, rawURL, nil)
	if err != nil {
		return "", 0, "", fmt.Errorf("build download request: %w", err)
	}
	request.Header.Set("User-Agent", "chatrepo-mcp/file-transfer")
	response, err := client.Do(request)
	if err != nil {
		return "", 0, "", fmt.Errorf("download file: %w", err)
	}
	defer response.Body.Close()
	if response.StatusCode < http.StatusOK || response.StatusCode >= http.StatusMultipleChoices {
		return "", 0, "", fmt.Errorf("file download returned HTTP %d", response.StatusCode)
	}
	if response.ContentLength > maxBytes {
		return "", 0, "", fmt.Errorf(
			"file exceeds FILE_TRANSFER_IMPORT_MAX_BYTES (%d > %d)",
			response.ContentLength, maxBytes,
		)
	}

	temp, err := os.CreateTemp(directory, ".chatrepo-upload-*")
	if err != nil {
		return "", 0, "", err
	}
	tempPath := temp.Name()
	completed := false
	defer func() {
		if !completed {
			_ = os.Remove(tempPath)
		}
	}()

	digest := sha256.New()
	written, copyErr := io.Copy(io.MultiWriter(temp, digest), io.LimitReader(response.Body, maxBytes+1))
	closeErr := temp.Close()
	if copyErr != nil {
		return "", 0, "", copyErr
	}
	if closeErr != nil {
		return "", 0, "", closeErr
	}
	if written > maxBytes {
		return "", 0, "", fmt.Errorf(
			"file exceeds FILE_TRANSFER_IMPORT_MAX_BYTES (%d > %d)",
			written, maxBytes,
		)
	}
	completed = true
	return tempPath, written, hex.EncodeToString(digest.Sum(nil)), nil
}

func hashFileLimited(path string, maxBytes int64) (int64, string, error) {
	file, err := os.Open(path)
	if err != nil {
		return 0, "", err
	}
	defer file.Close()
	digest := sha256.New()
	written, err := io.Copy(digest, io.LimitReader(file, maxBytes+1))
	if err != nil {
		return 0, "", err
	}
	if written > maxBytes {
		return 0, "", fmt.Errorf("file exceeds configured export limit")
	}
	return written, hex.EncodeToString(digest.Sum(nil)), nil
}

func safeTransferHTTPClient() *http.Client {
	transport := &http.Transport{
		Proxy:                 nil,
		DialContext:           safeTransferDialContext,
		TLSHandshakeTimeout:   15 * time.Second,
		ResponseHeaderTimeout: 30 * time.Second,
	}
	return &http.Client{
		Transport: transport,
		CheckRedirect: func(request *http.Request, via []*http.Request) error {
			if len(via) >= 10 {
				return fmt.Errorf("too many redirects")
			}
			_, err := validateTransferHTTPSURL(request.URL.String())
			return err
		},
	}
}

func validateTransferHTTPSURL(rawURL string) (*url.URL, error) {
	parsed, err := url.Parse(rawURL)
	if err != nil || parsed.Scheme != "https" || parsed.Hostname() == "" {
		return nil, fmt.Errorf("file download URL must be a valid HTTPS URL")
	}
	if parsed.User != nil {
		return nil, fmt.Errorf("file download URL must not contain credentials")
	}
	if port := parsed.Port(); port != "" && port != "443" {
		return nil, fmt.Errorf("file download URL must use the default HTTPS port")
	}
	return parsed, nil
}

func safeTransferDialContext(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	if port != "443" {
		return nil, fmt.Errorf("file download connection must use HTTPS port 443")
	}
	addresses, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(addresses) == 0 {
		if err == nil {
			err = fmt.Errorf("no addresses returned")
		}
		return nil, fmt.Errorf("resolve file download host %s: %w", host, err)
	}
	for _, address := range addresses {
		if !isPublicTransferIP(address) {
			return nil, fmt.Errorf("file download host resolves to a non-public address: %s", host)
		}
	}
	dialer := &net.Dialer{Timeout: 30 * time.Second, KeepAlive: 30 * time.Second}
	var lastErr error
	for _, resolved := range addresses {
		conn, dialErr := dialer.DialContext(ctx, network, net.JoinHostPort(resolved.String(), port))
		if dialErr == nil {
			return conn, nil
		}
		lastErr = dialErr
	}
	return nil, fmt.Errorf("connect to file download host %s: %w", host, lastErr)
}

func isPublicTransferIP(address netip.Addr) bool {
	if !address.IsValid() || !address.IsGlobalUnicast() || address.IsPrivate() ||
		address.IsLoopback() || address.IsLinkLocalUnicast() || address.IsLinkLocalMulticast() ||
		address.IsMulticast() || address.IsUnspecified() {
		return false
	}
	for _, prefix := range nonPublicTransferPrefixes {
		if prefix.Contains(address) {
			return false
		}
	}
	return true
}
