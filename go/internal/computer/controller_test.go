package computer

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestFitPNGDownscalesAndPreservesAspect(t *testing.T) {
	source := image.NewNRGBA(image.Rect(0, 0, 8, 4))
	for y := 0; y < 4; y++ {
		for x := 0; x < 8; x++ {
			source.Set(x, y, color.NRGBA{R: uint8(x * 20), G: uint8(y * 40), B: 100, A: 255})
		}
	}
	var encoded bytes.Buffer
	if err := png.Encode(&encoded, source); err != nil {
		t.Fatal(err)
	}

	data, width, height, err := fitPNG(encoded.Bytes(), 4)
	if err != nil {
		t.Fatal(err)
	}
	if width != 4 || height != 2 {
		t.Fatalf("fit dimensions = %dx%d, want 4x2", width, height)
	}
	decoded, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		t.Fatal(err)
	}
	if got := decoded.Bounds().Size(); got.X != 4 || got.Y != 2 {
		t.Fatalf("decoded size = %v", got)
	}
}

func TestMapPointUsesSnapshotPixelsAndNativeDesktopBounds(t *testing.T) {
	snapshot := &Snapshot{
		ImageWidth:  1000,
		ImageHeight: 500,
		Bounds: Rect{
			X: -1920, Y: 0, Width: 3840, Height: 1080,
		},
	}
	point, err := mapPoint(snapshot, 500, 250)
	if err != nil {
		t.Fatal(err)
	}
	if math.Abs(point.X-0) > 0.001 || math.Abs(point.Y-540) > 0.001 {
		t.Fatalf("mapped point = %+v, want (0,540)", point)
	}
	if _, err := mapPoint(snapshot, 1000, 250); err == nil {
		t.Fatal("out-of-bounds x was accepted")
	}
}

func TestSnapshotTTLRejectsStaleCoordinates(t *testing.T) {
	controller := &Controller{
		ttl:       5 * time.Millisecond,
		snapshots: map[string]*Snapshot{},
	}
	controller.snapshots["old"] = &Snapshot{ID: "old", CreatedAt: time.Now().Add(-time.Second)}
	if _, err := controller.snapshot(map[string]any{"snapshot_id": "old"}); err == nil {
		t.Fatal("expired snapshot was accepted")
	} else if typed, ok := err.(*DriverError); !ok || typed.Code != "stale_snapshot" {
		t.Fatalf("error = %#v", err)
	}
}

func TestInstallRuntimeAssetsExtractsOnlyCurrentPlatform(t *testing.T) {
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	assets, err := InstallRuntimeAssets()
	if err != nil {
		t.Fatal(err)
	}
	if assets.Driver == "" {
		t.Fatal("driver was not installed")
	}
	if info, err := os.Stat(assets.Driver); err != nil || !info.Mode().IsRegular() {
		t.Fatalf("driver stat = %#v err=%v", info, err)
	}
	if filepath.Dir(assets.Driver) != assets.Directory {
		t.Fatalf("driver %q is not in runtime dir %q", assets.Driver, assets.Directory)
	}
}

func TestDecodeProtocolErrorKeepsTypedDriverCode(t *testing.T) {
	code, message := decodeProtocolError(map[string]any{"code": "stale", "message": "refresh"})
	if code != "stale" || message != "refresh" {
		t.Fatalf("got %q %q", code, message)
	}
}

func TestControllerStartsLazyAndCapsSnapshotRAM(t *testing.T) {
	t.Setenv("XDG_CACHE_HOME", t.TempDir())
	controller, err := NewController(time.Second, time.Minute, time.Hour, 20, 1568)
	if err != nil {
		t.Fatal(err)
	}
	defer controller.Close()
	if controller.driver != nil || controller.portal != nil {
		t.Fatal("desktop drivers started before the first computer request")
	}

	for index := 0; index < maxRetainedSnapshots+3; index++ {
		snapshot := &Snapshot{
			ID:        fmt.Sprintf("snap-%d", index),
			Image:     []byte{byte(index)},
			CreatedAt: time.Now().Add(time.Duration(index) * time.Millisecond),
		}
		controller.rememberSnapshot(snapshot)
	}
	if got := len(controller.snapshots); got != maxRetainedSnapshots {
		t.Fatalf("retained snapshots = %d, want %d", got, maxRetainedSnapshots)
	}
	if _, ok := controller.snapshots["snap-0"]; ok {
		t.Fatal("oldest snapshot was not evicted")
	}
}

func TestCompactSequenceResultDropsImageAndScenePayloads(t *testing.T) {
	result := map[string]any{
		"ok":          true,
		"snapshot_id": "fresh",
		"image_b64":   strings.Repeat("x", 1024),
		"scene":       map[string]any{"windows": []any{1, 2, 3}},
		"verification": map[string]any{
			"status": "confirmed_changed",
		},
	}
	compact := compactSequenceResult(2, "click", result)
	if compact["snapshot_id"] != "fresh" || compact["operation"] != "click" || compact["step"] != 2 {
		t.Fatalf("compact receipt = %#v", compact)
	}
	if _, ok := compact["image_b64"]; ok {
		t.Fatal("sequence receipt retained image_b64")
	}
	if _, ok := compact["scene"]; ok {
		t.Fatal("sequence receipt retained full scene")
	}
}

func TestShareSnapshotReturnsCurrentFrameBytes(t *testing.T) {
	controller := &Controller{
		ttl:       time.Minute,
		snapshots: map[string]*Snapshot{},
	}
	snapshot := &Snapshot{
		ID:          "1234567890abcdef",
		Image:       []byte("png-bytes"),
		MIMEType:    "image/png",
		ImageWidth:  10,
		ImageHeight: 10,
		CreatedAt:   time.Now(),
	}
	controller.snapshots[snapshot.ID] = snapshot

	shared, err := controller.shareSnapshot(map[string]any{"snapshot_id": snapshot.ID})
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := base64.StdEncoding.DecodeString(shared["image_b64"].(string))
	if err != nil {
		t.Fatal(err)
	}
	if string(decoded) != string(snapshot.Image) {
		t.Fatalf("shared snapshot = %q", decoded)
	}
	if _, exists := shared["resource_uri"]; exists {
		t.Fatal("computer host should not create the public file ResourceLink")
	}
}
