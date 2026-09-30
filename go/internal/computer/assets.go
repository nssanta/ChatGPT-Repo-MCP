package computer

import (
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"runtime"
)

// driverAssets contains the platform helpers used by chatrepo-computer-host.
// The host extracts only the current platform's files into a versioned user cache.
//
//go:embed assets/linux/* assets/windows/* assets/macos/*
var driverAssets embed.FS

type RuntimeAssets struct {
	Directory string
	Driver    string
	Portal    string
}

func InstallRuntimeAssets() (RuntimeAssets, error) {
	subdir := runtime.GOOS
	switch runtime.GOOS {
	case "darwin":
		subdir = "macos"
	case "windows":
		subdir = "windows"
	case "linux":
		subdir = "linux"
	default:
		return RuntimeAssets{}, fmt.Errorf("unsupported_os: computer use is not supported on %s", runtime.GOOS)
	}
	root := "assets/" + subdir
	entries, err := fs.ReadDir(driverAssets, root)
	if err != nil {
		return RuntimeAssets{}, fmt.Errorf("read embedded computer assets: %w", err)
	}
	hash := sha256.New()
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		data, readErr := driverAssets.ReadFile(root + "/" + entry.Name())
		if readErr != nil {
			return RuntimeAssets{}, readErr
		}
		_, _ = hash.Write([]byte(entry.Name()))
		_, _ = hash.Write(data)
	}
	cache, err := os.UserCacheDir()
	if err != nil || cache == "" {
		home, homeErr := os.UserHomeDir()
		if homeErr != nil {
			return RuntimeAssets{}, fmt.Errorf("resolve computer runtime cache: %w", err)
		}
		cache = filepath.Join(home, ".cache")
	}
	version := hex.EncodeToString(hash.Sum(nil))[:16]
	dir := filepath.Join(cache, "chatrepo-mcp", "computer", version)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return RuntimeAssets{}, fmt.Errorf("create computer runtime directory: %w", err)
	}
	result := RuntimeAssets{Directory: dir}
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		data, readErr := driverAssets.ReadFile(root + "/" + entry.Name())
		if readErr != nil {
			return RuntimeAssets{}, readErr
		}
		target := filepath.Join(dir, entry.Name())
		current, _ := os.ReadFile(target)
		if string(current) != string(data) {
			tmp := target + ".tmp"
			if err := os.WriteFile(tmp, data, 0o700); err != nil {
				return RuntimeAssets{}, fmt.Errorf("write computer asset %s: %w", entry.Name(), err)
			}
			if err := os.Rename(tmp, target); err != nil {
				_ = os.Remove(tmp)
				return RuntimeAssets{}, fmt.Errorf("install computer asset %s: %w", entry.Name(), err)
			}
		}
		switch entry.Name() {
		case "computer_driver.py", "computer_driver.ps1", "ComputerDriver.swift", "computer_driver":
			result.Driver = target
		case "wayland_portal.py":
			result.Portal = target
		}
	}
	if result.Driver == "" {
		return RuntimeAssets{}, fmt.Errorf("computer driver asset is missing for %s", runtime.GOOS)
	}
	return result, nil
}
