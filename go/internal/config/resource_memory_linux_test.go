//go:build linux

package config

import (
	"errors"
	"testing"
)

func TestEffectiveMemoryUsesSmallestFiniteCgroupLimit(t *testing.T) {
	values := map[string]string{
		"/proc/self/cgroup":    "0::/system.slice/chatrepo.service\n",
		"/proc/self/mountinfo": "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
		"/sys/fs/cgroup/system.slice/chatrepo.service/memory.max":  "8589934592\n",
		"/sys/fs/cgroup/system.slice/chatrepo.service/memory.high": "4294967296\n",
	}
	read := func(path string) ([]byte, error) {
		value, ok := values[path]
		if !ok {
			return nil, errors.New("missing")
		}
		return []byte(value), nil
	}
	if got := effectiveMemoryBytes(16*gibibyte, read); got != 4*gibibyte {
		t.Fatalf("effective memory=%d, want %d", got, 4*gibibyte)
	}
}

func TestEffectiveMemoryIgnoresMissingInvalidAndUnlimitedLimits(t *testing.T) {
	read := func(path string) ([]byte, error) {
		switch path {
		case "/proc/self/cgroup":
			return []byte("0::/service\n"), nil
		case "/proc/self/mountinfo":
			return []byte("36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n"), nil
		case "/sys/fs/cgroup/service/memory.max":
			return []byte("max\n"), nil
		case "/sys/fs/cgroup/service/memory.high":
			return []byte("invalid\n"), nil
		default:
			return nil, errors.New("not mounted")
		}
	}
	if got := effectiveMemoryBytes(16*gibibyte, read); got != 16*gibibyte {
		t.Fatalf("effective memory=%d, want host memory", got)
	}
}

func TestEffectiveMemoryResolvesV1MemoryController(t *testing.T) {
	values := map[string]string{
		"/proc/self/cgroup":                               "5:cpu,memory:/docker/abc\n",
		"/proc/self/mountinfo":                            "40 25 0:35 /docker /sys/fs/cgroup/memory rw - cgroup cgroup rw,memory\n",
		"/sys/fs/cgroup/memory/abc/memory.limit_in_bytes": "3221225472\n",
	}
	read := func(path string) ([]byte, error) {
		value, ok := values[path]
		if !ok {
			return nil, errors.New("missing")
		}
		return []byte(value), nil
	}
	if got := effectiveMemoryBytes(16*gibibyte, read); got != 3*gibibyte {
		t.Fatalf("effective memory=%d, want %d", got, 3*gibibyte)
	}
}

func TestEffectiveMemoryRejectsTraversal(t *testing.T) {
	values := map[string]string{
		"/proc/self/cgroup":    "0::/../../escape\n",
		"/proc/self/mountinfo": "36 25 0:32 / /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
	}
	read := func(path string) ([]byte, error) { return []byte(values[path]), nil }
	if got := effectiveMemoryBytes(16*gibibyte, read); got != 16*gibibyte {
		t.Fatalf("traversal changed effective memory: %d", got)
	}
}

func TestEffectiveMemoryNamespaceRootMapsToMountPoint(t *testing.T) {
	values := map[string]string{
		"/proc/self/cgroup":          "0::/\n",
		"/proc/self/mountinfo":       "36 25 0:32 /system.slice/chatrepo.service /sys/fs/cgroup rw - cgroup2 cgroup rw\n",
		"/sys/fs/cgroup/memory.max":  "2147483648\n",
		"/sys/fs/cgroup/memory.high": "max\n",
	}
	read := func(path string) ([]byte, error) { return []byte(values[path]), nil }
	if got := effectiveMemoryBytes(16*gibibyte, read); got != 2*gibibyte {
		t.Fatalf("effective memory=%d, want %d", got, 2*gibibyte)
	}
}
