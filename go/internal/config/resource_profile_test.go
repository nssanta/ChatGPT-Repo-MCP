package config

import (
	"os"
	"testing"
)

func TestResolveResourceProfilePresetsAndCustom(t *testing.T) {
	cases := []struct {
		name   string
		buffer int64
		heavy  int
	}{{"small", 16 * 1024 * 1024, 2}, {"medium", 32 * 1024 * 1024, 4}, {"large", 64 * 1024 * 1024, 8}}
	for _, item := range cases {
		profile, buffer, heavy, _, err := resolveResourceProfile(item.name, 0, 0)
		if err != nil || profile != item.name || buffer != item.buffer || heavy != item.heavy {
			t.Fatalf("%s: profile=%s buffer=%d heavy=%d err=%v", item.name, profile, buffer, heavy, err)
		}
	}
	profile, buffer, heavy, _, err := resolveResourceProfile("custom", 12345, 3)
	if err != nil || profile != "custom" || buffer != 12345 || heavy != 3 {
		t.Fatalf("custom: %s %d %d %v", profile, buffer, heavy, err)
	}
	if _, _, _, _, err := resolveResourceProfile("custom", 0, 0); err == nil {
		t.Fatal("invalid custom profile accepted")
	}
	profile, buffer, heavy, _, err = resolveResourceProfile("custom", 0, 3)
	if err != nil || profile != "custom" || buffer != 16*1024*1024 || heavy != 3 {
		t.Fatalf("custom diagnostic default: %s %d %d %v", profile, buffer, heavy, err)
	}
}

func TestOptionalResourceOverridesRejectNonPositiveValues(t *testing.T) {
	for _, value := range []string{"0", "-1", "invalid"} {
		t.Setenv("RESOURCE_BUFFER_BYTES", value)
		if _, _, err := optionalPositiveIntEnv("RESOURCE_BUFFER_BYTES"); err == nil {
			t.Fatalf("RESOURCE_BUFFER_BYTES=%q was accepted", value)
		}
	}
	os.Unsetenv("RESOURCE_BUFFER_BYTES")
	if value, present, err := optionalPositiveIntEnv("RESOURCE_BUFFER_BYTES"); err != nil || present || value != 0 {
		t.Fatalf("unset override: value=%d present=%v err=%v", value, present, err)
	}
}

func TestResolveResourceProfileAutoIsTruthfulOrSmallFallback(t *testing.T) {
	profile, buffer, heavy, detected, err := resolveResourceProfile("auto", 0, 0)
	if err != nil {
		t.Fatal(err)
	}
	if detected == 0 && profile != "small" {
		t.Fatalf("unknown memory must fail safe to small: %s", profile)
	}
	if buffer <= 0 || heavy <= 0 {
		t.Fatalf("invalid applied limits: %d %d", buffer, heavy)
	}
}
