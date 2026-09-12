package codegen_test

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"testing"

	"github.com/goccy/wasm2go/internal/codegen"
	"github.com/goccy/wasm2go/internal/wasm"
)

// readPrebuilt parses a prebuilt testdata/<name>.wasm (one that carries a
// name section, which wat2wasm-built fixtures do not).
func readPrebuilt(t *testing.T, name string) *wasm.Module {
	t.Helper()
	bin, err := os.ReadFile(filepath.Join("..", "..", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	mod, err := wasm.Parse(bytes.NewReader(bin))
	if err != nil {
		t.Fatalf("parse %s: %v", name, err)
	}
	return mod
}

var funcDeclRe = regexp.MustCompile(`(?m)^func (F_\w+|Fn\d+)\(`)

// symbolLayout translates mod with SymbolNames and returns, for every
// generated wasm function, the chunk file that holds its body.
func symbolLayout(t *testing.T, mod *wasm.Module, chunks int) (map[string]string, codegen.Result) {
	t.Helper()
	res, err := codegen.Translate(nil, mod, codegen.Options{
		Package: "wmod", OutputImportPath: "gentest/wmod",
		PureOnly: true, SymbolNames: true, Chunks: chunks,
	})
	if err != nil {
		t.Fatalf("translate: %v", err)
	}
	owner := map[string]string{}
	for rel, data := range res.Files {
		if !strings.HasPrefix(rel, "p") || strings.Contains(rel, "alias") {
			continue
		}
		for _, m := range funcDeclRe.FindAllStringSubmatch(string(data), -1) {
			owner[m[1]] = rel
		}
	}
	return owner, res
}

func TestSymbolNamesStableAcrossIndexShift(t *testing.T) {
	restore := codegen.SetMultiPackageThreshold(0)
	defer restore()

	before, res := symbolLayout(t, readPrebuilt(t, "symnames.wasm"), 3)
	after, _ := symbolLayout(t, readPrebuilt(t, "symnames2.wasm"), 3)

	want := []string{"F_square", "F_twice", "F_sum_of_squares", "F_report", "F_store_at"}
	for _, name := range want {
		if _, ok := before[name]; !ok {
			t.Errorf("%s: not generated (have %v)", name, keys(before))
			continue
		}
		if after[name] != before[name] {
			t.Errorf("%s: moved from %s to %s after the index shift", name, before[name], after[name])
		}
	}
	for name := range before {
		if strings.HasPrefix(name, "Fn") {
			t.Errorf("%s: index-based name emitted although the name section covers every function", name)
		}
	}
	if _, ok := after["F_extra"]; !ok {
		t.Errorf("F_extra: not generated in the shifted module (have %v)", keys(after))
	}
	if len(before) != len(want) || len(after) != len(want)+1 {
		t.Errorf("function counts: before=%d after=%d", len(before), len(after))
	}

	// The layout must also be a buildable package tree.
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "go.mod"), []byte("module gentest\n\ngo 1.25.0\n"), 0644); err != nil {
		t.Fatal(err)
	}
	for rel, data := range res.Files {
		p := filepath.Join(dir, "wmod", rel)
		if err := os.MkdirAll(filepath.Dir(p), 0755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0644); err != nil {
			t.Fatal(err)
		}
	}
	for name, data := range res.Sidecars {
		if err := os.WriteFile(filepath.Join(dir, "wmod", name), data, 0644); err != nil {
			t.Fatal(err)
		}
	}
	cmd := exec.Command("go", "build", "./...")
	cmd.Dir = dir
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("go build failed: %v\n%s", err, out)
	}
}

func TestSymbolNamesRequirePure(t *testing.T) {
	_, err := codegen.Translate(nil, readPrebuilt(t, "symnames.wasm"), codegen.Options{
		Package: "wmod", OutputImportPath: "gentest/wmod", SymbolNames: true,
	})
	if err == nil || !strings.Contains(err.Error(), "PureOnly") {
		t.Fatalf("want a PureOnly error, got %v", err)
	}
}

func keys(m map[string]string) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}
