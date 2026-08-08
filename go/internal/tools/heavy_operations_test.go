package tools

import (
	"testing"
	"time"
)

func TestHeavyOperationsAreListedAndCancelled(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.heavySlots = make(chan struct{}, 1)
	cancelled := make(chan struct{})
	lease, acquired := engine.acquireHeavyOperation(heavyOperationSpec{
		Tool: "run_command", CWD: root, RequestID: "request-1",
	})
	if !acquired {
		t.Fatal("expected heavy lease")
	}
	lease.SetCancel(func() { close(cancelled) })
	listed := engine.listHeavyOperations()
	if listed["used"] != 1 {
		t.Fatalf("unexpected heavy status: %#v", listed)
	}
	operations := listed["operations"].([]map[string]any)
	if len(operations) != 1 || operations[0]["operation_id"] != lease.ID() || operations[0]["tool"] != "run_command" {
		t.Fatalf("unexpected operations: %#v", operations)
	}
	result := engine.cancelHeavyOperation(lease.ID())
	if result["ok"] != true || result["cancel_requested"] != true {
		t.Fatalf("unexpected cancel result: %#v", result)
	}
	select {
	case <-cancelled:
	case <-time.After(time.Second):
		t.Fatal("cancel callback was not called")
	}
	lease.Release()
	if engine.listHeavyOperations()["used"] != 0 {
		t.Fatal("released operation remained visible")
	}
}

func TestHeavyBusyResultIncludesActiveOperations(t *testing.T) {
	engine, _ := newTestEngine(t)
	engine.heavySlots = make(chan struct{}, 1)
	lease, acquired := engine.acquireHeavyOperation(heavyOperationSpec{Tool: "search_text", RequestID: "search-1"})
	if !acquired {
		t.Fatal("expected heavy lease")
	}
	defer lease.Release()
	busy := engine.heavyBusyResult()
	operations := busy["operations"].([]map[string]any)
	if len(operations) != 1 || operations[0]["tool"] != "search_text" {
		t.Fatalf("unexpected busy operations: %#v", operations)
	}
}
