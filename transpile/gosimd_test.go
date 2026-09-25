package transpile_test

// End-to-end check of the -simd=<target> backend: the cg_simd fixture
// is translated once with Options.SIMD and run twice from the same
// output tree — under GOEXPERIMENT=simd (the vector variant, archsimd
// registers) and without it (the pair variant) — and both must
// reproduce the wazero-verified expectations of the fixture test.

import (
	"bytes"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/goccy/wasm2go/internal/testfixture"
	"github.com/goccy/wasm2go/transpile"
)

func TestGoSIMDDifferential(t *testing.T) {
	if runtime.GOARCH != "amd64" && runtime.GOARCH != "arm64" {
		t.Skip("archsimd backend has no helper set for " + runtime.GOARCH)
	}
	ver, err := exec.Command("go", "env", "GOVERSION").Output()
	if err != nil {
		t.Fatal(err)
	}
	if v := strings.TrimSpace(string(ver)); !strings.HasPrefix(v, "go1.27") {
		t.Skipf("-simd=go127 needs a Go 1.27 toolchain to exercise the vector variant (have %s)", v)
	}

	bin := testfixture.Wasm(t, "cg_simd.wasm")
	m, err := transpile.Parse(bytes.NewReader(bin))
	if err != nil {
		t.Fatal(err)
	}
	restore := transpile.SetMultiPackageThreshold(0)
	defer restore()
	var buf bytes.Buffer
	res, err := transpile.Translate(&buf, m, transpile.Options{
		Package: "pkg", OutputImportPath: "simdtest/pkg", PureOnly: true, SIMD: "go127",
	})
	if err != nil {
		t.Fatalf("translate: %v", err)
	}
	dir := t.TempDir()
	w := func(rel string, data []byte) {
		p := filepath.Join(dir, rel)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, data, 0o644); err != nil {
			t.Fatal(err)
		}
	}
	w("go.mod", []byte("module simdtest\n\ngo 1.25.0\n"))
	if buf.Len() > 0 {
		w("pkg/gen.go", buf.Bytes())
	}
	var gosimdFiles, nosimdFiles int
	for _, set := range []map[string][]byte{res.Files, res.Sidecars, res.AuxFiles} {
		for name, data := range set {
			if len(data) == 0 {
				continue
			}
			if strings.HasSuffix(name, "_gosimd.go") {
				gosimdFiles++
			}
			if strings.HasSuffix(name, "_nosimd.go") {
				nosimdFiles++
			}
			w("pkg/"+name, data)
		}
	}
	if gosimdFiles == 0 || nosimdFiles == 0 {
		t.Fatalf("expected tagged variant files, got %d _gosimd.go and %d _nosimd.go", gosimdFiles, nosimdFiles)
	}
	if _, ok := res.Files["base/simd_g_go127_amd64.go"]; !ok {
		t.Fatalf("base/simd_g_go127_amd64.go missing from %v", keys(res.Files))
	}
	w("main.go", []byte(`package main

import (
	"fmt"

	"simdtest/pkg"
)

func main() {
	m := pkg.New()
	fmt.Println(pkg.Intarith(m, -123456, 789), pkg.Widen(m, -32768, 32767), pkg.Memv(m, 0), pkg.Shuf(m, 0x55), pkg.Cmpmask(m, -5, 3))
}
`))
	// Same expectations as TestGcasmSimdDifferential.
	const want = "1646524174 2147451134 437725748 176 131342"
	for _, env := range [][]string{{"GOEXPERIMENT=simd"}, {"GOEXPERIMENT="}} {
		cmd := exec.Command("go", "run", ".")
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), env...)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("go run %v: %v\n%s", env, err, out)
		}
		if got := strings.TrimSpace(string(out)); got != want {
			t.Errorf("env %v: got %q, want %q", env, got, want)
		}
	}
}

func keys(m map[string][]byte) []string {
	var out []string
	for k := range m {
		out = append(out, k)
	}
	return out
}
