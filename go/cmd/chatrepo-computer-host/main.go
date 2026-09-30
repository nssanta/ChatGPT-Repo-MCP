package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/nssanta/ChatGPT-Repo-MCP/go/internal/computer"
)

type request struct {
	ID     int            `json:"id"`
	Method string         `json:"method"`
	Params map[string]any `json:"params"`
}

type response struct {
	ID     int            `json:"id"`
	Result map[string]any `json:"result,omitempty"`
	Error  *errorBody     `json:"error,omitempty"`
}

type errorBody struct {
	Code    string `json:"code"`
	Message string `json:"message"`
}

func main() {
	timeout := durationEnv("COMPUTER_ACTION_TIMEOUT_MS", 30*time.Second)
	ttl := durationEnv("COMPUTER_SNAPSHOT_TTL_SECONDS", 30*time.Second)
	idleTimeout := time.Duration(intEnv("COMPUTER_IDLE_TIMEOUT_SECONDS", 300)) * time.Second
	maxSteps := intEnv("COMPUTER_MAX_SEQUENCE_STEPS", 20)
	maxEdge := intEnv("COMPUTER_CAPTURE_MAX_EDGE", 1568)
	controlEnabled := boolEnv("COMPUTER_CONTROL_ENABLED", false)
	controller, err := computer.NewController(timeout, ttl, idleTimeout, maxSteps, maxEdge)
	if err != nil {
		write(response{ID: 0, Error: &errorBody{Code: "startup_failed", Message: err.Error()}})
		os.Exit(1)
	}
	defer controller.Close()

	scanner := bufio.NewScanner(os.Stdin)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		var req request
		if err := json.Unmarshal([]byte(line), &req); err != nil {
			write(response{ID: 0, Error: &errorBody{Code: "bad_request", Message: "request is not valid JSON: " + err.Error()}})
			continue
		}
		if req.Params == nil {
			req.Params = map[string]any{}
		}
		if isControlMethod(req.Method) && !controlEnabled {
			write(response{ID: req.ID, Error: &errorBody{Code: "control_disabled", Message: "COMPUTER_CONTROL_ENABLED=false"}})
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), maxDuration(timeout, 3*time.Minute))
		result, callErr := controller.Call(ctx, req.Method, req.Params)
		cancel()
		if callErr != nil {
			code := "failed"
			var driverErr *computer.DriverError
			if errors.As(callErr, &driverErr) && driverErr.Code != "" {
				code = driverErr.Code
			}
			write(response{ID: req.ID, Error: &errorBody{Code: code, Message: callErr.Error()}})
			continue
		}
		write(response{ID: req.ID, Result: result})
	}
}

func write(value response) {
	data, err := json.Marshal(value)
	if err != nil {
		data = []byte(fmt.Sprintf(`{"id":%d,"error":{"code":"failed","message":"result serialization failed"}}`, value.ID))
	}
	_, _ = os.Stdout.Write(append(data, '\n'))
}

func durationEnv(name string, fallback time.Duration) time.Duration {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	if name == "COMPUTER_SNAPSHOT_TTL_SECONDS" {
		if value, err := strconv.Atoi(raw); err == nil && value > 0 {
			return time.Duration(value) * time.Second
		}
		return fallback
	}
	if value, err := strconv.Atoi(raw); err == nil && value > 0 {
		return time.Duration(value) * time.Millisecond
	}
	return fallback
}

func intEnv(name string, fallback int) int {
	raw := strings.TrimSpace(os.Getenv(name))
	if raw == "" {
		return fallback
	}
	value, err := strconv.Atoi(raw)
	if err != nil || value <= 0 {
		return fallback
	}
	return value
}

func maxDuration(a, b time.Duration) time.Duration {
	if a > b {
		return a
	}
	return b
}

func boolEnv(name string, fallback bool) bool {
	raw := strings.ToLower(strings.TrimSpace(os.Getenv(name)))
	if raw == "" {
		return fallback
	}
	switch raw {
	case "1", "true", "yes", "y", "on":
		return true
	case "0", "false", "no", "n", "off":
		return false
	default:
		return fallback
	}
}

func isControlMethod(method string) bool {
	switch method {
	case "element", "click", "move", "type", "key", "scroll", "drag", "window", "launch", "sequence":
		return true
	default:
		return false
	}
}
