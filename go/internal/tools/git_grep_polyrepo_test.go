package tools

import (
	"context"
	"os"
	"path/filepath"
	"testing"
)

func TestGitGrepFansOutAcrossPolyrepoWorkspace(t *testing.T) {
	engine, root := newTestEngine(t)
	engine.settings.WorkspaceScanDepth = 2

	for _, name := range []string{"repo-a", "repo-b"} {
		repo := filepath.Join(root, name)
		if err := os.MkdirAll(repo, 0o755); err != nil {
			t.Fatal(err)
		}
		runGitTest(t, repo, "init", "-b", "main")
		if err := os.WriteFile(filepath.Join(repo, "match.txt"), []byte("shared-needle\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		runGitTest(t, repo, "add", "match.txt")
		runGitTest(t, repo, "-c", "user.email=test@example.com", "-c", "user.name=Tester", "commit", "-m", "init")
	}

	result := engine.Execute(context.Background(), "git_grep", map[string]any{
		"query": "shared-needle",
	})
	if result["polyrepo"] != true || result["count"] != 2 {
		t.Fatalf("git_grep did not fan out: %#v", result)
	}
	repos, ok := result["repos_searched"].([]string)
	if !ok || len(repos) != 2 {
		t.Fatalf("repos_searched = %#v, want two repos", result["repos_searched"])
	}
	matches, ok := result["results"].([]map[string]any)
	if !ok || len(matches) != 2 {
		t.Fatalf("results = %#v, want two matches", result["results"])
	}
	for _, match := range matches {
		if match["repo"] == "" || match["path"] != "match.txt" {
			t.Fatalf("match lacks polyrepo identity: %#v", match)
		}
	}
}
