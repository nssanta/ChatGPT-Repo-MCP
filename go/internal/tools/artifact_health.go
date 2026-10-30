package tools

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"time"
)

func (e *Engine) artifactStorageHealth(ctx context.Context) map[string]any {
	result := map[string]any{"ok": false}
	if err := ctx.Err(); err != nil {
		result["error"] = fmt.Errorf("check artifact storage: %w", err).Error()
		return result
	}
	store, err := e.artifactStore()
	if err != nil {
		result["error"] = fmt.Errorf("initialize artifact storage: %w", err).Error()
		return result
	}
	store.mu.Lock()
	defer store.mu.Unlock()
	result["reserve_bytes"] = store.reserve
	available, capacity, err := artifactDiskSpace(store.root)
	if err != nil {
		result["error"] = fmt.Errorf("check artifact disk space: %w", err).Error()
		return result
	}
	store.diskAvailable, store.diskCapacity, store.diskChecked = available, capacity, time.Now()
	result["available_bytes"] = available
	result["capacity_bytes"] = capacity
	if available <= store.reserve {
		result["error"] = fmt.Sprintf("artifact_quota: disk reserve %d bytes would be crossed", store.reserve)
		return result
	}
	if err := probeArtifactStorage(ctx, store.root); err != nil {
		result["error"] = err.Error()
		return result
	}
	result["ok"] = true
	return result
}

func probeArtifactStorage(ctx context.Context, root string) (err error) {
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("check artifact storage: %w", err)
	}
	file, err := os.CreateTemp(root, ".health-*")
	if err != nil {
		return fmt.Errorf("create artifact storage probe: %w", err)
	}
	defer func() {
		if closeErr := file.Close(); closeErr != nil {
			err = errors.Join(err, fmt.Errorf("close artifact storage probe: %w", closeErr))
		}
		if removeErr := os.Remove(file.Name()); removeErr != nil {
			err = errors.Join(err, fmt.Errorf("remove artifact storage probe: %w", removeErr))
		}
	}()
	if n, err := file.Write([]byte{0}); err != nil {
		return fmt.Errorf("write artifact storage probe: %w", err)
	} else if n != 1 {
		return fmt.Errorf("write artifact storage probe: %w", io.ErrShortWrite)
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("check artifact storage: %w", err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("sync artifact storage probe: %w", err)
	}
	if err := ctx.Err(); err != nil {
		return fmt.Errorf("check artifact storage: %w", err)
	}
	return nil
}
