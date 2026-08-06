//go:build !linux && !windows

package config

func detectPhysicalMemoryBytes() int64 { return 0 }
