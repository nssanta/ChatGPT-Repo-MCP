//go:build linux

package config

import (
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"

	"golang.org/x/sys/unix"
)

func detectPhysicalMemoryBytes() int64 {
	var info unix.Sysinfo_t
	if unix.Sysinfo(&info) != nil {
		return 0
	}
	host := int64(info.Totalram) * int64(info.Unit)
	return effectiveMemoryBytes(host, os.ReadFile)
}

func effectiveMemoryBytes(host int64, readFile func(string) ([]byte, error)) int64 {
	if host <= 0 {
		return host
	}
	effective := host
	for _, limitPath := range cgroupMemoryLimitPaths(readFile) {
		data, err := readFile(limitPath)
		if err != nil {
			continue
		}
		value := strings.TrimSpace(string(data))
		if value == "max" {
			continue
		}
		limit, err := strconv.ParseInt(value, 10, 64)
		if err == nil && limit > 0 && limit < effective {
			effective = limit
		}
	}
	return effective
}

func cgroupMemoryLimitPaths(readFile func(string) ([]byte, error)) []string {
	membershipData, err := readFile("/proc/self/cgroup")
	if err != nil {
		return nil
	}
	mountData, err := readFile("/proc/self/mountinfo")
	if err != nil {
		return nil
	}
	var v2Path, v1Path string
	for _, line := range strings.Split(string(membershipData), "\n") {
		parts := strings.SplitN(line, ":", 3)
		if len(parts) != 3 {
			continue
		}
		if parts[1] == "" {
			v2Path = parts[2]
		} else if containsWord(parts[1], "memory") {
			v1Path = parts[2]
		}
	}
	var result []string
	for _, line := range strings.Split(string(mountData), "\n") {
		fields := strings.Fields(line)
		separator := -1
		for index, field := range fields {
			if field == "-" {
				separator = index
				break
			}
		}
		if separator < 6 || len(fields) <= separator+3 {
			continue
		}
		filesystem, superOptions := fields[separator+1], fields[separator+3]
		group := v2Path
		if filesystem == "cgroup" {
			if !containsWord(superOptions, "memory") {
				continue
			}
			group = v1Path
		} else if filesystem != "cgroup2" {
			continue
		}
		directory, ok := safeCgroupDir(fields[3], fields[4], group)
		if !ok {
			continue
		}
		if filesystem == "cgroup2" {
			result = append(result, filepath.Join(directory, "memory.max"), filepath.Join(directory, "memory.high"))
		} else {
			result = append(result, filepath.Join(directory, "memory.limit_in_bytes"))
		}
	}
	return result
}

func safeCgroupDir(mountRoot, mountPoint, group string) (string, bool) {
	for _, value := range []string{mountRoot, mountPoint, group} {
		if !path.IsAbs(value) || strings.Contains(value, "..") {
			return "", false
		}
	}
	relative := "."
	if group != "/" {
		if group != mountRoot {
			var ok bool
			relative, ok = strings.CutPrefix(group, strings.TrimSuffix(mountRoot, "/")+"/")
			if !ok {
				return "", false
			}
		}
	}
	candidate := filepath.Clean(filepath.Join(mountPoint, filepath.FromSlash(relative)))
	rel, err := filepath.Rel(filepath.Clean(mountPoint), candidate)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return candidate, true
}

func containsWord(commaSeparated, wanted string) bool {
	for _, item := range strings.Split(commaSeparated, ",") {
		if item == wanted {
			return true
		}
	}
	return false
}
