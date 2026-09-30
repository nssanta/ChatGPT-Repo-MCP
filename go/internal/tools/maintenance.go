package tools

import (
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

type maintenanceResult struct {
	ArtifactsChecked    bool
	AuditFilesRemoved   int
	RuntimeFilesRemoved int
}

func (e *Engine) startMaintenance() {
	if !e.settings.MaintenanceEnabled || e.maintenanceStop != nil {
		return
	}
	e.maintenanceStop = make(chan struct{})
	e.maintenanceWG.Add(1)
	go func() {
		defer e.maintenanceWG.Done()
		interval := e.settings.MaintenanceInterval
		initial := min(interval, time.Minute)
		timer := time.NewTimer(initial)
		defer timer.Stop()

		select {
		case <-e.maintenanceStop:
			return
		case <-timer.C:
		}

		for {
			_ = e.runMaintenanceOnce(time.Now())
			timer.Reset(interval)
			select {
			case <-e.maintenanceStop:
				return
			case <-timer.C:
			}
		}
	}()
}

func (e *Engine) stopMaintenance() {
	e.maintenanceStopOnce.Do(func() {
		if e.maintenanceStop != nil {
			close(e.maintenanceStop)
		}
	})
	e.maintenanceWG.Wait()
}

func (e *Engine) runMaintenanceOnce(now time.Time) maintenanceResult {
	result := maintenanceResult{}
	if info, err := os.Stat(e.settings.CommandJobsDir); err == nil && info.IsDir() {
		if store, storeErr := e.artifactStore(); storeErr == nil {
			if cleanupErr := store.cleanup(); cleanupErr == nil {
				result.ArtifactsChecked = true
			}
		}
		result.RuntimeFilesRemoved = cleanupStaleRuntimeFiles(
			e.settings.CommandJobsDir,
			now.Add(-e.settings.ArtifactTTL),
		)
	}
	result.AuditFilesRemoved = cleanupRotatedAuditLogs(
		e.settings.CommandAuditLogPath,
		now.Add(-e.settings.AuditLogTTL),
	)
	if dir, err := computerShareDirectory(); err == nil {
		result.RuntimeFilesRemoved += cleanupComputerShareDirectory(dir, now)
	}
	return result
}

func cleanupRotatedAuditLogs(path string, cutoff time.Time) int {
	directory := filepath.Dir(path)
	base := filepath.Base(path)
	entries, err := os.ReadDir(directory)
	if err != nil {
		return 0
	}
	removed := 0
	prefix := base + "."
	for _, entry := range entries {
		if entry.IsDir() || !strings.HasPrefix(entry.Name(), prefix) {
			continue
		}
		suffix := strings.TrimPrefix(entry.Name(), prefix)
		if _, err := strconv.Atoi(suffix); err != nil {
			continue
		}
		full := filepath.Join(directory, entry.Name())
		info, err := entry.Info()
		if err != nil || !info.ModTime().Before(cutoff) {
			continue
		}
		if err := os.Remove(full); err == nil || errors.Is(err, os.ErrNotExist) {
			removed++
		}
	}
	return removed
}

func cleanupStaleRuntimeFiles(root string, cutoff time.Time) int {
	// Job/concurrency locks are owned by their PID-aware lifecycle and are not
	// deleted by age here: artifact retention may be shorter than a live job.
	candidates := make([]string, 0)
	for _, directory := range []string{root, filepath.Join(root, "artifacts"), filepath.Join(root, "logs")} {
		entries, err := os.ReadDir(directory)
		if err != nil {
			continue
		}
		for _, entry := range entries {
			if !entry.IsDir() && strings.HasSuffix(entry.Name(), ".tmp") {
				candidates = append(candidates, filepath.Join(directory, entry.Name()))
			}
		}
	}
	removed := 0
	seen := make(map[string]struct{}, len(candidates))
	for _, candidate := range candidates {
		if _, ok := seen[candidate]; ok {
			continue
		}
		seen[candidate] = struct{}{}
		info, err := os.Stat(candidate)
		if err != nil || !info.ModTime().Before(cutoff) {
			continue
		}
		if err := os.Remove(candidate); err == nil || errors.Is(err, os.ErrNotExist) {
			removed++
		}
	}
	return removed
}
