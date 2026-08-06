//go:build !windows

package tools

import (
	"os"

	"golang.org/x/sys/unix"
)

func artifactDiskSpace(path string) (int64, int64, error) {
	var stat unix.Statfs_t
	if err := unix.Statfs(path, &stat); err != nil {
		return 0, 0, err
	}
	return int64(stat.Bavail) * int64(stat.Bsize), int64(stat.Blocks) * int64(stat.Bsize), nil
}

func replaceArtifactFile(source, target string) error { return os.Rename(source, target) }
