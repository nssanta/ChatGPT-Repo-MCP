//go:build windows

package tools

import "golang.org/x/sys/windows"

func artifactDiskSpace(path string) (int64, int64, error) {
	root, err := windows.UTF16PtrFromString(path)
	if err != nil {
		return 0, 0, err
	}
	var available, capacity, free uint64
	if err := windows.GetDiskFreeSpaceEx(root, &available, &capacity, &free); err != nil {
		return 0, 0, err
	}
	return int64(available), int64(capacity), nil
}

func replaceArtifactFile(source, target string) error {
	from, err := windows.UTF16PtrFromString(source)
	if err != nil {
		return err
	}
	to, err := windows.UTF16PtrFromString(target)
	if err != nil {
		return err
	}
	return windows.MoveFileEx(from, to, windows.MOVEFILE_REPLACE_EXISTING|windows.MOVEFILE_WRITE_THROUGH)
}
