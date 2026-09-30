package computer

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"image"
	"image/png"
	"math"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

type Rect struct {
	X      float64 `json:"x"`
	Y      float64 `json:"y"`
	Width  float64 `json:"width"`
	Height float64 `json:"height"`
}

const maxRetainedSnapshots = 8

type Snapshot struct {
	ID             string
	Image          []byte
	MIMEType       string
	ImageWidth     int
	ImageHeight    int
	Bounds         Rect
	CreatedAt      time.Time
	ActiveWindowID string
	ImageHash      string
}

type Controller struct {
	mu          sync.Mutex
	assets      RuntimeAssets
	driver      *lineProcess
	portal      *lineProcess
	snapshots   map[string]*Snapshot
	ttl         time.Duration
	idleTimeout time.Duration
	maxSteps    int
	maxEdge     int
	timeout     time.Duration
	lastStatus  map[string]any
	lastActive  time.Time
	stopIdle    chan struct{}
	closeOnce   sync.Once
}

func NewController(timeout, ttl, idleTimeout time.Duration, maxSteps, maxEdge int) (*Controller, error) {
	assets, err := InstallRuntimeAssets()
	if err != nil {
		return nil, err
	}
	controller := &Controller{
		assets: assets, snapshots: make(map[string]*Snapshot),
		ttl: ttl, idleTimeout: idleTimeout, maxSteps: maxSteps, maxEdge: maxEdge, timeout: timeout,
		stopIdle: make(chan struct{}),
	}
	go controller.idleLoop()
	return controller, nil
}

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

func (c *Controller) ensureRuntimeLocked() error {
	if c.driver != nil && !c.driver.running() {
		c.driver.close()
		c.driver = nil
	}
	if c.portal != nil && !c.portal.running() {
		c.portal.close()
		c.portal = nil
	}
	if c.driver == nil {
		driver, err := newDriverProcess(c.assets, c.timeout)
		if err != nil {
			return err
		}
		c.driver = driver
	}
	if runtime.GOOS != "linux" || !isWayland() || c.portal != nil {
		return nil
	}
	portal, portalErr := newPortalProcess(c.assets, maxDuration(c.timeout, 150*time.Second))
	if portalErr != nil {
		c.driver.close()
		c.driver = nil
		return portalErr
	}
	c.portal = portal
	return nil
}

func (c *Controller) releaseRuntimeLocked() {
	if c.portal != nil {
		c.portal.close()
		c.portal = nil
	}
	if c.driver != nil {
		c.driver.close()
		c.driver = nil
	}
	c.snapshots = map[string]*Snapshot{}
	c.lastStatus = nil
	c.lastActive = time.Time{}
}

func (c *Controller) idleLoop() {
	if c.idleTimeout <= 0 {
		return
	}
	interval := c.idleTimeout / 4
	if interval > 30*time.Second {
		interval = 30 * time.Second
	}
	if interval < time.Second {
		interval = time.Second
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		select {
		case <-c.stopIdle:
			return
		case now := <-ticker.C:
			c.mu.Lock()
			if c.driver != nil && !c.lastActive.IsZero() && now.Sub(c.lastActive) >= c.idleTimeout {
				c.releaseRuntimeLocked()
			}
			c.mu.Unlock()
		}
	}
}

func (c *Controller) Close() {
	c.closeOnce.Do(func() {
		close(c.stopIdle)
		c.mu.Lock()
		defer c.mu.Unlock()
		c.releaseRuntimeLocked()
	})
}

func (c *Controller) Call(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.cleanupSnapshots()
	if err := c.ensureRuntimeLocked(); err != nil {
		return nil, err
	}
	c.lastActive = time.Now()
	result, err := c.callLocked(ctx, method, params)
	c.lastActive = time.Now()
	return result, err
}

func (c *Controller) callLocked(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	switch method {
	case "status":
		return c.status(ctx)
	case "observe":
		return c.observe(ctx, params)
	case "zoom":
		return c.zoom(ctx, params)
	case "windows":
		return c.driverMap(ctx, "windows", map[string]any{})
	case "elements":
		return c.driverMap(ctx, "elements", params)
	case "element":
		return c.element(ctx, params)
	case "click":
		return c.click(ctx, params)
	case "move":
		return c.move(ctx, params)
	case "type":
		return c.typeText(ctx, params)
	case "key":
		return c.key(ctx, params)
	case "scroll":
		return c.scroll(ctx, params)
	case "drag":
		return c.drag(ctx, params)
	case "window":
		return c.window(ctx, params)
	case "launch":
		return c.launch(ctx, params)
	case "wait":
		return c.wait(ctx, params)
	case "sequence":
		return c.sequence(ctx, params)
	default:
		return nil, &DriverError{Code: "bad_request", Message: "unknown computer host method: " + method}
	}
}

func (c *Controller) status(ctx context.Context) (map[string]any, error) {
	var hello map[string]any
	if err := c.driver.call(ctx, "hello", map[string]any{}, &hello); err != nil {
		return nil, err
	}
	captureAvailable := capabilityBool(hello, "capture")
	controlAvailable := capabilityBool(hello, "input")
	result := map[string]any{
		"ok": true, "platform": runtime.GOOS, "session": sessionKind(),
		"backend": hello["backend"], "capabilities": hello["capabilities"],
		"permissions": hello["permissions"], "notes": hello["notes"],
		"runtime_dir": c.assets.Directory,
	}
	if runtime.GOOS == "linux" && isWayland() && c.portal != nil {
		var probe map[string]any
		if err := c.portal.call(ctx, "probe", map[string]any{}, &probe); err == nil {
			captureAvailable = true
			controlAvailable = true
			result["portal"] = probe
			result["backend"] = "linux-wayland-portal+atspi"
		} else {
			result["portal"] = map[string]any{"supported": false, "error": err.Error()}
		}
	}
	result["capture"] = captureAvailable
	result["control"] = controlAvailable
	c.lastStatus = result
	return result, nil
}

func sessionKind() string {
	if runtime.GOOS == "linux" {
		if isWayland() {
			return "wayland"
		}
		if strings.TrimSpace(getenv("DISPLAY")) != "" {
			return "x11"
		}
	}
	return runtime.GOOS
}

var getenv = os.Getenv

func capabilityBool(hello map[string]any, name string) bool {
	caps, _ := hello["capabilities"].(map[string]any)
	value, _ := caps[name].(bool)
	return value
}

func (c *Controller) observe(ctx context.Context, params map[string]any) (map[string]any, error) {
	var region *Rect
	if raw, ok := params["region"].(map[string]any); ok {
		rect, err := rectFromMap(raw)
		if err != nil {
			return nil, err
		}
		region = &rect
	}
	capture, err := c.capture(ctx, region)
	if err != nil {
		return nil, err
	}
	imageB64, _ := capture["image_b64"].(string)
	if imageB64 == "" {
		return nil, &DriverError{Code: "capture_failed", Message: "computer driver returned no image"}
	}
	imageBytes, err := base64.StdEncoding.DecodeString(imageB64)
	if err != nil {
		return nil, &DriverError{Code: "capture_failed", Message: "computer driver returned invalid base64 image"}
	}
	width := intValue(capture["image_width"], 0)
	height := intValue(capture["image_height"], 0)
	if width <= 0 || height <= 0 {
		return nil, &DriverError{Code: "capture_failed", Message: "computer driver returned invalid image dimensions"}
	}
	bounds := boundsFromCapture(capture, width, height)
	if stringValue(capture["mime_type"], "image/png") == "image/png" {
		if resized, resizedWidth, resizedHeight, resizeErr := fitPNG(imageBytes, c.maxEdge); resizeErr == nil {
			imageBytes = resized
			width, height = resizedWidth, resizedHeight
			imageB64 = base64.StdEncoding.EncodeToString(imageBytes)
		}
	}
	snapshot := &Snapshot{
		ID: randomID(), Image: imageBytes, MIMEType: stringValue(capture["mime_type"], "image/png"),
		ImageWidth: width, ImageHeight: height, Bounds: bounds, CreatedAt: time.Now().UTC(),
		ImageHash: hashBytes(imageBytes),
	}
	includeOCR, _ := params["include_ocr"].(bool)
	scene := c.buildScene(ctx, snapshot, includeOCR)
	if active, ok := scene["active"].(map[string]any); ok {
		if window, ok := active["window"].(map[string]any); ok {
			snapshot.ActiveWindowID = stringValue(window["id"], "")
		}
	}
	c.rememberSnapshot(snapshot)
	result := map[string]any{
		"ok": true, "snapshot_id": snapshot.ID, "mime_type": snapshot.MIMEType,
		"image_b64": imageB64, "image_width": width, "image_height": height,
		"bounds": bounds, "captured_at": snapshot.CreatedAt.Format(time.RFC3339Nano),
		"expires_at": snapshot.CreatedAt.Add(c.ttl).Format(time.RFC3339Nano),
		"scene":      scene,
	}
	if backend := capture["backend"]; backend != nil {
		result["backend"] = backend
	}
	return result, nil
}

func (c *Controller) capture(ctx context.Context, region *Rect) (map[string]any, error) {
	params := map[string]any{}
	if region != nil {
		params["region"] = map[string]any{"x": region.X, "y": region.Y, "width": region.Width, "height": region.Height}
	}
	var result map[string]any
	if c.portal != nil {
		if err := c.portal.call(ctx, "capture", params, &result); err != nil {
			return nil, err
		}
		if _, ok := result["mime_type"]; !ok {
			result["mime_type"] = "image/png"
		}
		return result, nil
	}
	if err := c.driver.call(ctx, "capture", params, &result); err != nil {
		return nil, err
	}
	return result, nil
}

func boundsFromCapture(capture map[string]any, width, height int) Rect {
	if raw, ok := capture["bounds"].(map[string]any); ok {
		if rect, err := rectFromMap(raw); err == nil {
			return rect
		}
	}
	if raw, ok := capture["region"].(map[string]any); ok {
		if rect, err := rectFromMap(raw); err == nil {
			return rect
		}
	}
	logicalW := floatValue(capture["logical_width"], float64(width))
	logicalH := floatValue(capture["logical_height"], float64(height))
	return Rect{X: 0, Y: 0, Width: logicalW, Height: logicalH}
}

func (c *Controller) buildScene(ctx context.Context, snapshot *Snapshot, includeOCR bool) map[string]any {
	scene := map[string]any{"coherent": true}
	windows, windowsErr := c.driverMap(ctx, "windows", map[string]any{})
	var active map[string]any
	if windowsErr == nil {
		scene["windows"] = windows["windows"]
		if list, ok := windows["windows"].([]any); ok {
			for _, item := range list {
				window, _ := item.(map[string]any)
				if focused, _ := window["focused"].(bool); focused {
					active = window
					break
				}
			}
		}
	} else {
		scene["windows_error"] = windowsErr.Error()
	}
	var focused map[string]any
	if err := c.driver.call(ctx, "focused", map[string]any{}, &focused); err == nil {
		scene["focus"] = focused
	}
	var cursor map[string]any
	if err := c.driver.call(ctx, "cursor", map[string]any{}, &cursor); err == nil {
		scene["cursor"] = cursor
	}
	elementParams := map[string]any{"max": 200, "depth": 12}
	if active != nil && active["id"] != nil {
		elementParams["window_id"] = active["id"]
	}
	if elements, err := c.driverMap(ctx, "elements", elementParams); err == nil {
		scene["accessibility"] = elements
	} else {
		scene["accessibility"] = map[string]any{"status": "unavailable", "error": err.Error()}
	}
	if includeOCR {
		var hello map[string]any
		if err := c.driver.call(ctx, "hello", map[string]any{}, &hello); err == nil && capabilityBool(hello, "ocr") {
			var ocr map[string]any
			if err := c.driver.call(ctx, "ocr", map[string]any{"image_b64": base64.StdEncoding.EncodeToString(snapshot.Image), "languages": []string{"ru", "en"}}, &ocr); err == nil {
				scene["ocr"] = ocr
			} else {
				scene["ocr"] = map[string]any{"status": "unavailable", "error": err.Error()}
			}
		} else {
			scene["ocr"] = map[string]any{"status": "unavailable"}
		}
	}
	scene["active"] = map[string]any{"window": active, "focused": focused}
	return scene
}

func (c *Controller) zoom(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, err := c.snapshot(params)
	if err != nil {
		return nil, err
	}
	x := floatValue(params["x"], math.NaN())
	y := floatValue(params["y"], math.NaN())
	width := floatValue(params["width"], math.NaN())
	height := floatValue(params["height"], math.NaN())
	if !finitePositive(width) || !finitePositive(height) || math.IsNaN(x) || math.IsNaN(y) || x < 0 || y < 0 || x+width > float64(snapshot.ImageWidth) || y+height > float64(snapshot.ImageHeight) {
		return nil, &DriverError{Code: "bad_request", Message: "zoom region is outside the snapshot"}
	}
	region := Rect{
		X:      snapshot.Bounds.X + x/float64(snapshot.ImageWidth)*snapshot.Bounds.Width,
		Y:      snapshot.Bounds.Y + y/float64(snapshot.ImageHeight)*snapshot.Bounds.Height,
		Width:  width / float64(snapshot.ImageWidth) * snapshot.Bounds.Width,
		Height: height / float64(snapshot.ImageHeight) * snapshot.Bounds.Height,
	}
	observeParams := map[string]any{"region": map[string]any{"x": region.X, "y": region.Y, "width": region.Width, "height": region.Height}}
	if includeOCR, ok := params["include_ocr"].(bool); ok {
		observeParams["include_ocr"] = includeOCR
	}
	return c.observe(ctx, observeParams)
}

func (c *Controller) element(ctx context.Context, params map[string]any) (map[string]any, error) {
	action := stringValue(params["action"], "press")
	id := stringValue(params["element_id"], "")
	if id == "" {
		return nil, &DriverError{Code: "bad_request", Message: "element_id is required"}
	}
	raw := map[string]any{"id": id, "action": action}
	if params["value"] != nil {
		raw["value"] = params["value"]
	}
	var result map[string]any
	if err := c.driver.call(ctx, "element_action", raw, &result); err != nil {
		return nil, err
	}
	if action == "read" || action == "locate" {
		result["ok"] = true
		return result, nil
	}
	return c.observeAfter(ctx, result, "")
}

func (c *Controller) click(ctx context.Context, params map[string]any) (map[string]any, error) {
	if id := stringValue(params["element_id"], ""); id != "" {
		return c.element(ctx, map[string]any{"element_id": id, "action": "press"})
	}
	snapshot, point, err := c.pointFromSnapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	action := map[string]any{
		"action": "click", "x": point.X, "y": point.Y,
		"button": stringValue(params["button"], "left"), "clicks": intValue(params["clicks"], 1),
	}
	if params["modifiers"] != nil {
		action["modifiers"] = params["modifiers"]
	}
	if params["hold_ms"] != nil {
		action["hold_ms"] = params["hold_ms"]
	}
	before := snapshot.ImageHash
	if err := c.input(ctx, action); err != nil {
		return nil, err
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true}, before)
}

func (c *Controller) move(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, point, err := c.pointFromSnapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	if err := c.input(ctx, map[string]any{"action": "move", "x": point.X, "y": point.Y}); err != nil {
		return nil, err
	}
	hoverMS := intValue(params["hover_ms"], 250)
	if hoverMS > 0 {
		time.Sleep(time.Duration(min(hoverMS, 5000)) * time.Millisecond)
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true, "hover_ms": hoverMS}, snapshot.ImageHash)
}

func (c *Controller) typeText(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, err := c.snapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	text := stringValue(params["text"], "")
	if text == "" && params["clear"] != true && params["submit"] != true {
		return nil, &DriverError{Code: "bad_request", Message: "text is empty"}
	}
	if params["clear"] == true {
		mod := "control"
		if runtime.GOOS == "darwin" {
			mod = "command"
		}
		if err := c.input(ctx, map[string]any{"action": "key", "key": "a", "modifiers": []string{mod}}); err != nil {
			return nil, err
		}
	}
	if text != "" {
		if err := c.input(ctx, map[string]any{"action": "type", "text": text}); err != nil {
			return nil, err
		}
	}
	if params["submit"] == true {
		if err := c.input(ctx, map[string]any{"action": "key", "key": "enter"}); err != nil {
			return nil, err
		}
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true, "characters": len([]rune(text))}, "")
}

func (c *Controller) key(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, err := c.snapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	key := stringValue(params["key"], "")
	if key == "" {
		return nil, &DriverError{Code: "bad_request", Message: "key is required"}
	}
	modifiers := stringSlice(params["modifiers"])
	if len(modifiers) == 0 && strings.Contains(key, "+") {
		parts := strings.Split(key, "+")
		key = parts[len(parts)-1]
		modifiers = parts[:len(parts)-1]
	}
	action := map[string]any{"action": "key", "key": key, "repeat": intValue(params["repeat"], 1)}
	if len(modifiers) > 0 {
		action["modifiers"] = modifiers
	}
	if err := c.input(ctx, action); err != nil {
		return nil, err
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true}, "")
}

func (c *Controller) scroll(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, point, err := c.pointFromSnapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	dx := floatValue(params["delta_x"], 0)
	dy := floatValue(params["delta_y"], 0)
	if direction := stringValue(params["direction"], ""); direction != "" {
		amount := float64(intValue(params["amount"], 3))
		switch direction {
		case "up":
			dy = -amount
		case "down":
			dy = amount
		case "left":
			dx = -amount
		case "right":
			dx = amount
		default:
			return nil, &DriverError{Code: "bad_request", Message: "direction must be up, down, left or right"}
		}
		params["unit"] = "line"
	}
	action := map[string]any{"action": "scroll", "x": point.X, "y": point.Y, "deltaX": dx, "deltaY": dy, "unit": stringValue(params["unit"], "pixel")}
	if params["modifiers"] != nil {
		action["modifiers"] = params["modifiers"]
	}
	if err := c.input(ctx, action); err != nil {
		return nil, err
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true}, "")
}

func (c *Controller) drag(ctx context.Context, params map[string]any) (map[string]any, error) {
	snapshot, start, err := c.pointFromSnapshot(params)
	if err != nil {
		return nil, err
	}
	if err := c.validateSnapshotContext(ctx, snapshot); err != nil {
		return nil, err
	}
	toX := floatValue(params["to_x"], math.NaN())
	toY := floatValue(params["to_y"], math.NaN())
	if math.IsNaN(toX) || math.IsNaN(toY) {
		return nil, &DriverError{Code: "bad_request", Message: "to_x and to_y are required"}
	}
	end, err := mapPoint(snapshot, toX, toY)
	if err != nil {
		return nil, err
	}
	action := map[string]any{
		"action": "drag", "x": start.X, "y": start.Y, "toX": end.X, "toY": end.Y,
		"button": stringValue(params["button"], "left"),
	}
	if params["duration_ms"] != nil {
		action["duration_ms"] = params["duration_ms"]
	}
	if params["steps"] != nil {
		action["steps"] = params["steps"]
	}
	if params["modifiers"] != nil {
		action["modifiers"] = params["modifiers"]
	}
	if err := c.input(ctx, action); err != nil {
		return nil, err
	}
	return c.observeAfter(ctx, map[string]any{"delivered": true}, snapshot.ImageHash)
}

func (c *Controller) window(ctx context.Context, params map[string]any) (map[string]any, error) {
	raw := map[string]any{"id": params["window_id"], "action": params["action"]}
	for _, key := range []string{"x", "y", "width", "height"} {
		if params[key] != nil {
			if raw["bounds"] == nil {
				raw["bounds"] = map[string]any{}
			}
			raw["bounds"].(map[string]any)[key] = params[key]
		}
	}
	var result map[string]any
	if err := c.driver.call(ctx, "window", raw, &result); err != nil {
		return nil, err
	}
	return c.observeAfter(ctx, result, "")
}

func (c *Controller) launch(ctx context.Context, params map[string]any) (map[string]any, error) {
	raw := map[string]any{"app": params["app"]}
	if params["args"] != nil {
		raw["args"] = params["args"]
	}
	var result map[string]any
	if err := c.driver.call(ctx, "launch", raw, &result); err != nil {
		return nil, err
	}
	time.Sleep(250 * time.Millisecond)
	return c.observeAfter(ctx, result, "")
}

func (c *Controller) wait(ctx context.Context, params map[string]any) (map[string]any, error) {
	until := stringValue(params["until"], "stable")
	timeout := time.Duration(intValue(params["timeout_ms"], 10000)) * time.Millisecond
	if timeout <= 0 {
		timeout = 10 * time.Second
	}
	deadline := time.Now().Add(timeout)
	query := strings.ToLower(stringValue(params["query"], ""))
	var lastHash string
	stableSince := time.Time{}
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		if time.Now().After(deadline) {
			return nil, &DriverError{Code: "timeout", Message: "computer_wait condition was not met"}
		}
		switch until {
		case "time":
			ms := time.Duration(intValue(params["ms"], 1000)) * time.Millisecond
			if ms > time.Until(deadline) {
				ms = time.Until(deadline)
			}
			time.Sleep(ms)
			return map[string]any{"ok": true, "met": true, "until": until}, nil
		case "window", "window_gone":
			windows, err := c.driverMap(ctx, "windows", map[string]any{})
			if err == nil {
				found := searchJSON(windows["windows"], query)
				if (until == "window" && found) || (until == "window_gone" && !found) {
					windows["ok"] = true
					windows["met"] = true
					return windows, nil
				}
			}
		case "element", "element_gone":
			elementParams := map[string]any{"query": query, "max": 50, "depth": 12}
			if role := stringValue(params["role"], ""); role != "" {
				elementParams["role"] = role
			}
			elements, err := c.driverMap(ctx, "elements", elementParams)
			if err == nil {
				found := false
				if list, ok := elements["elements"].([]any); ok {
					found = len(list) > 0
				}
				if (until == "element" && found) || (until == "element_gone" && !found) {
					elements["ok"] = true
					elements["met"] = true
					return elements, nil
				}
			}
		default:
			if until == "text" || until == "text_gone" {
				shot, err := c.observe(ctx, map[string]any{})
				if err == nil {
					found := searchJSON(shot["scene"], query)
					if (until == "text" && found) || (until == "text_gone" && !found) {
						shot["met"] = true
						return shot, nil
					}
				}
			} else {
				capture, err := c.capture(ctx, nil)
				if err == nil {
					b64, _ := capture["image_b64"].(string)
					data, decErr := base64.StdEncoding.DecodeString(b64)
					hash := ""
					if decErr == nil {
						hash = hashBytes(data)
					}
					if hash != "" && hash == lastHash {
						if stableSince.IsZero() {
							stableSince = time.Now()
						}
						if time.Since(stableSince) >= time.Duration(intValue(params["stable_ms"], 800))*time.Millisecond {
							shot, observeErr := c.observe(ctx, map[string]any{})
							if observeErr != nil {
								return nil, observeErr
							}
							shot["met"] = true
							shot["until"] = "stable"
							return shot, nil
						}
					} else {
						lastHash = hash
						stableSince = time.Time{}
					}
				}
			}
		}
		time.Sleep(150 * time.Millisecond)
	}
}

func (c *Controller) sequence(ctx context.Context, params map[string]any) (map[string]any, error) {
	steps, ok := params["steps"].([]any)
	if !ok || len(steps) == 0 {
		return nil, &DriverError{Code: "bad_request", Message: "steps must be a non-empty array"}
	}
	if len(steps) > c.maxSteps {
		return nil, &DriverError{Code: "bad_request", Message: fmt.Sprintf("steps exceeds COMPUTER_MAX_SEQUENCE_STEPS (%d)", c.maxSteps)}
	}
	results := make([]any, 0, len(steps))
	var final map[string]any
	for index, raw := range steps {
		step, ok := raw.(map[string]any)
		if !ok {
			return nil, &DriverError{Code: "bad_request", Message: fmt.Sprintf("step %d is not an object", index)}
		}
		method := strings.TrimPrefix(stringValue(step["operation"], ""), "computer_")
		args, _ := step["arguments"].(map[string]any)
		if method == "" {
			return nil, &DriverError{Code: "bad_request", Message: fmt.Sprintf("step %d has no operation", index)}
		}
		allowed := map[string]bool{
			"element": true, "click": true, "move": true, "type": true, "key": true,
			"scroll": true, "drag": true, "window": true, "launch": true, "wait": true,
		}
		if !allowed[method] {
			return nil, &DriverError{Code: "bad_request", Message: fmt.Sprintf("step %d uses unsupported sequence operation %q", index, method)}
		}
		var err error
		final, err = c.callLocked(ctx, method, args)
		if err != nil {
			return map[string]any{"ok": false, "failed_step": index, "results": results}, err
		}
		results = append(results, compactSequenceResult(index, method, final))
	}
	if final == nil {
		final = map[string]any{"ok": true}
	}
	final["ok"] = true
	final["sequence_results"] = results
	return final, nil
}

func compactSequenceResult(index int, operation string, result map[string]any) map[string]any {
	compact := map[string]any{"step": index, "operation": operation, "ok": result["ok"]}
	for _, key := range []string{"snapshot_id", "verification", "action_result", "met", "until"} {
		if value, ok := result[key]; ok {
			compact[key] = value
		}
	}
	return compact
}

func (c *Controller) input(ctx context.Context, action map[string]any) error {
	if c.portal != nil {
		var result map[string]any
		return c.portal.call(ctx, "action", action, &result)
	}
	var result map[string]any
	return c.driver.call(ctx, "input", action, &result)
}

func (c *Controller) observeAfter(ctx context.Context, result map[string]any, beforeHash string) (map[string]any, error) {
	time.Sleep(120 * time.Millisecond)
	shot, err := c.observe(ctx, map[string]any{})
	if err != nil {
		result["ok"] = true
		result["verification"] = map[string]any{"status": "uncertain", "reason": err.Error()}
		return result, nil
	}
	afterHash := ""
	if b64, ok := shot["image_b64"].(string); ok {
		if data, decErr := base64.StdEncoding.DecodeString(b64); decErr == nil {
			afterHash = hashBytes(data)
		}
	}
	status := "observed"
	changed := beforeHash != "" && afterHash != "" && beforeHash != afterHash
	if beforeHash != "" {
		if changed {
			status = "confirmed_changed"
		} else {
			status = "uncertain_unchanged"
		}
	}
	shot["action_result"] = result
	shot["verification"] = map[string]any{"status": status, "visual_changed": changed}
	return shot, nil
}

func (c *Controller) driverMap(ctx context.Context, method string, params map[string]any) (map[string]any, error) {
	var result map[string]any
	if err := c.driver.call(ctx, method, params, &result); err != nil {
		return nil, err
	}
	result["ok"] = true
	return result, nil
}

type Point struct {
	X float64
	Y float64
}

func (c *Controller) pointFromSnapshot(params map[string]any) (*Snapshot, Point, error) {
	snapshot, err := c.snapshot(params)
	if err != nil {
		return nil, Point{}, err
	}
	point, err := mapPoint(snapshot, floatValue(params["x"], math.NaN()), floatValue(params["y"], math.NaN()))
	return snapshot, point, err
}

func mapPoint(snapshot *Snapshot, x, y float64) (Point, error) {
	if math.IsNaN(x) || math.IsNaN(y) || x < 0 || y < 0 || x >= float64(snapshot.ImageWidth) || y >= float64(snapshot.ImageHeight) {
		return Point{}, &DriverError{Code: "bad_request", Message: "coordinates are outside snapshot bounds"}
	}
	return Point{
		X: snapshot.Bounds.X + x/float64(snapshot.ImageWidth)*snapshot.Bounds.Width,
		Y: snapshot.Bounds.Y + y/float64(snapshot.ImageHeight)*snapshot.Bounds.Height,
	}, nil
}

func (c *Controller) validateSnapshotContext(ctx context.Context, snapshot *Snapshot) error {
	if snapshot.ActiveWindowID == "" || c.portal != nil {
		return nil
	}
	windows, err := c.driverMap(ctx, "windows", map[string]any{})
	if err != nil {
		return nil
	}
	list, _ := windows["windows"].([]any)
	for _, item := range list {
		window, _ := item.(map[string]any)
		focused, _ := window["focused"].(bool)
		if focused {
			current := stringValue(window["id"], "")
			if current != "" && current != snapshot.ActiveWindowID {
				return &DriverError{Code: "stale_snapshot", Message: "the active window changed since this snapshot; observe the computer again"}
			}
			return nil
		}
	}
	return nil
}

func (c *Controller) rememberSnapshot(snapshot *Snapshot) {
	c.cleanupSnapshots()
	if len(c.snapshots) >= maxRetainedSnapshots {
		oldestID := ""
		oldest := time.Now()
		for id, item := range c.snapshots {
			if oldestID == "" || item.CreatedAt.Before(oldest) {
				oldestID = id
				oldest = item.CreatedAt
			}
		}
		if oldestID != "" {
			delete(c.snapshots, oldestID)
		}
	}
	c.snapshots[snapshot.ID] = snapshot
}

func (c *Controller) snapshot(params map[string]any) (*Snapshot, error) {
	id := stringValue(params["snapshot_id"], "")
	if id == "" {
		return nil, &DriverError{Code: "bad_request", Message: "snapshot_id is required"}
	}
	snapshot := c.snapshots[id]
	if snapshot == nil {
		return nil, &DriverError{Code: "stale_snapshot", Message: "snapshot is unknown or expired; observe the computer again"}
	}
	if time.Since(snapshot.CreatedAt) > c.ttl {
		delete(c.snapshots, id)
		return nil, &DriverError{Code: "stale_snapshot", Message: "snapshot expired; observe the computer again"}
	}
	return snapshot, nil
}

func (c *Controller) cleanupSnapshots() {
	now := time.Now()
	for id, snapshot := range c.snapshots {
		if now.Sub(snapshot.CreatedAt) > c.ttl {
			delete(c.snapshots, id)
		}
	}
}

func rectFromMap(raw map[string]any) (Rect, error) {
	rect := Rect{
		X: floatValue(raw["x"], math.NaN()), Y: floatValue(raw["y"], math.NaN()),
		Width: floatValue(raw["width"], math.NaN()), Height: floatValue(raw["height"], math.NaN()),
	}
	if math.IsNaN(rect.X) || math.IsNaN(rect.Y) || !finitePositive(rect.Width) || !finitePositive(rect.Height) {
		return Rect{}, &DriverError{Code: "bad_request", Message: "invalid rectangle"}
	}
	return rect, nil
}

func finitePositive(value float64) bool {
	return !math.IsNaN(value) && !math.IsInf(value, 0) && value > 0
}

func floatValue(value any, fallback float64) float64 {
	switch typed := value.(type) {
	case float64:
		return typed
	case float32:
		return float64(typed)
	case int:
		return float64(typed)
	case int64:
		return float64(typed)
	case json.Number:
		if parsed, err := typed.Float64(); err == nil {
			return parsed
		}
	}
	return fallback
}

func stringValue(value any, fallback string) string {
	if value == nil {
		return fallback
	}
	if text, ok := value.(string); ok && text != "" {
		return text
	}
	return fallback
}

func stringSlice(value any) []string {
	raw, _ := value.([]any)
	if direct, ok := value.([]string); ok {
		return direct
	}
	result := make([]string, 0, len(raw))
	for _, item := range raw {
		if text, ok := item.(string); ok {
			result = append(result, text)
		}
	}
	return result
}

func randomID() string {
	data := make([]byte, 16)
	_, _ = rand.Read(data)
	return hex.EncodeToString(data)
}

func hashBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func fitPNG(data []byte, maxEdge int) ([]byte, int, int, error) {
	decoded, err := png.Decode(bytes.NewReader(data))
	if err != nil {
		return nil, 0, 0, err
	}
	bounds := decoded.Bounds()
	width, height := bounds.Dx(), bounds.Dy()
	if width <= 0 || height <= 0 {
		return nil, 0, 0, fmt.Errorf("invalid PNG dimensions")
	}
	if maxEdge <= 0 || max(width, height) <= maxEdge {
		return data, width, height, nil
	}
	scale := float64(maxEdge) / float64(max(width, height))
	outWidth := max(1, int(math.Round(float64(width)*scale)))
	outHeight := max(1, int(math.Round(float64(height)*scale)))
	out := image.NewNRGBA(image.Rect(0, 0, outWidth, outHeight))
	for y := 0; y < outHeight; y++ {
		sourceY := bounds.Min.Y + min(height-1, int(float64(y)*float64(height)/float64(outHeight)))
		for x := 0; x < outWidth; x++ {
			sourceX := bounds.Min.X + min(width-1, int(float64(x)*float64(width)/float64(outWidth)))
			out.Set(x, y, decoded.At(sourceX, sourceY))
		}
	}
	var buffer bytes.Buffer
	encoder := png.Encoder{CompressionLevel: png.BestSpeed}
	if err := encoder.Encode(&buffer, out); err != nil {
		return nil, 0, 0, err
	}
	return buffer.Bytes(), outWidth, outHeight, nil
}

func searchJSON(value any, query string) bool {
	if query == "" {
		return false
	}
	data, _ := json.Marshal(value)
	return strings.Contains(strings.ToLower(string(data)), query)
}
