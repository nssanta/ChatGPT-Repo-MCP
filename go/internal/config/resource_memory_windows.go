//go:build windows

package config

import (
	"golang.org/x/sys/windows"
	"unsafe"
)

type memoryStatusEx struct {
	length, memoryLoad                                                                                   uint32
	totalPhys, availPhys, totalPageFile, availPageFile, totalVirtual, availVirtual, availExtendedVirtual uint64
}

func detectPhysicalMemoryBytes() int64 {
	var status memoryStatusEx
	status.length = uint32(unsafe.Sizeof(status))
	proc := windows.NewLazySystemDLL("kernel32.dll").NewProc("GlobalMemoryStatusEx")
	result, _, _ := proc.Call(uintptr(unsafe.Pointer(&status)))
	if result == 0 {
		return 0
	}
	return int64(status.totalPhys)
}
