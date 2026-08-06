package config

import "fmt"

const gibibyte = int64(1024 * 1024 * 1024)

func resolveResourceProfile(profile string, customBuffer int64, customHeavy int) (string, int64, int, int64, error) {
	detected := detectPhysicalMemoryBytes()
	if profile == "auto" {
		switch {
		case detected <= 4*gibibyte:
			profile = "small"
		case detected <= 16*gibibyte:
			profile = "medium"
		default:
			profile = "large"
		}
	}
	var buffer int64
	var heavy int
	switch profile {
	case "small":
		buffer, heavy = 16*1024*1024, 2
	case "medium":
		buffer, heavy = 32*1024*1024, 4
	case "large":
		buffer, heavy = 64*1024*1024, 8
	case "custom":
		if customHeavy <= 0 {
			return "", 0, 0, detected, fmt.Errorf("RESOURCE_PROFILE=custom requires positive MAX_HEAVY_OPERATIONS")
		}
		// Сохраняем поле как диагностическую оценку для обратной совместимости.
		buffer, heavy = 16*1024*1024, customHeavy
		if customBuffer > 0 {
			buffer = customBuffer
		}
	default:
		return "", 0, 0, detected, fmt.Errorf("RESOURCE_PROFILE must be auto, small, medium, large, or custom")
	}
	if profile != "custom" {
		if customBuffer > 0 {
			buffer = customBuffer
		}
		if customHeavy > 0 {
			heavy = customHeavy
		}
	}
	return profile, buffer, heavy, detected, nil
}
