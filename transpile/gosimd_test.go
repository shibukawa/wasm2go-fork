package transpile_test

// End-to-end checks of the -simd=<target> backend. A fixture is
// translated once with Options.SIMD and run twice from the same output
// tree — under GOEXPERIMENT=simd (the vector variant, archsimd
// registers) and without it (the pair variant) — and the two runs must
// agree. cg_simd also has wazero-verified expectations; the memory64
// fixture and the grouped-files layout are checked differentially.

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

func goSIMDToolchain(t *testing.T) {
	t.Helper()
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
}

// buildGoSIMD translates fixture with Options.SIMD (plus extra) into a
// module directory holding main.go, and returns the directory.
func buildGoSIMD(t *testing.T, fixture string, extra func(*transpile.Options), mainGo string) string {
	t.Helper()
	bin := testfixture.Wasm(t, fixture)
	m, err := transpile.Parse(bytes.NewReader(bin))
	if err != nil {
		t.Fatal(err)
	}
	restore := transpile.SetMultiPackageThreshold(0)
	defer restore()
	opts := transpile.Options{Package: "pkg", OutputImportPath: "simdtest/pkg", PureOnly: true, SIMD: "go127"}
	if extra != nil {
		extra(&opts)
	}
	var buf bytes.Buffer
	res, err := transpile.Translate(&buf, m, opts)
	if err != nil {
		t.Fatalf("translate %s: %v", fixture, err)
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
	helper := "base/simd_g_go127_" + runtime.GOARCH + ".go"
	if _, ok := res.Files[helper]; !ok {
		t.Fatalf("%s missing from the output", helper)
	}
	w("main.go", []byte(mainGo))
	return dir
}

// runGoSIMD runs the module both ways and returns the two outputs.
func runGoSIMD(t *testing.T, dir string) (vector, pair string) {
	t.Helper()
	run := func(env string) string {
		cmd := exec.Command("go", "run", ".")
		cmd.Dir = dir
		cmd.Env = append(os.Environ(), env)
		out, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("go run %s: %v\n%s", env, err, out)
		}
		return strings.TrimSpace(string(out))
	}
	return run("GOEXPERIMENT=simd"), run("GOEXPERIMENT=")
}

const simdMain = `package main

import (
	"fmt"

	"simdtest/pkg"
)

func main() {
	m := pkg.New()
	fmt.Println(pkg.Intarith(m, -123456, 789), pkg.Widen(m, -32768, 32767), pkg.Memv(m, 0), pkg.Shuf(m, 0x55), pkg.Cmpmask(m, -5, 3))
}
`

func TestGoSIMDDifferential(t *testing.T) {
	goSIMDToolchain(t)
	dir := buildGoSIMD(t, "cg_simd.wasm", nil, simdMain)
	// Same expectations as TestGcasmSimdDifferential (wazero-verified).
	const want = "1646524174 2147451134 437725748 176 131342"
	vector, pair := runGoSIMD(t, dir)
	if vector != want || pair != want {
		t.Errorf("vector %q, pair %q, want %q", vector, pair, want)
	}
}

// The grouped-files layout routes v128 functions to <stem>_gosimd.go /
// <stem>_nosimd.go next to their group.
func TestGoSIMDGroupFiles(t *testing.T) {
	goSIMDToolchain(t)
	dir := buildGoSIMD(t, "cg_simd.wasm", func(o *transpile.Options) {
		o.SymbolNames, o.GroupFiles, o.Chunks, o.AddrConsts = true, true, 1, true
	}, simdMain)
	const want = "1646524174 2147451134 437725748 176 131342"
	vector, pair := runGoSIMD(t, dir)
	if vector != want || pair != want {
		t.Errorf("vector %q, pair %q, want %q", vector, pair, want)
	}
}

// memory64: the simd_m64_* helper family, checked differentially over
// the fixture's memory after its SIMD store loop.
func TestGoSIMDMemory64(t *testing.T) {
	goSIMDToolchain(t)
	dir := buildGoSIMD(t, "cg_mem64_simd_store.wasm", nil, `package main

import (
	"fmt"
	"hash/crc32"

	"simdtest/pkg"
)

func main() {
	m := pkg.New()
	mem := pkg.Memory(m)
	for i := range mem[:4096] {
		mem[i] = byte(i*7 + 3)
	}
	pkg.Scale2(m, 64)
	fmt.Println(crc32.ChecksumIEEE(mem[:4096]))
}
`)
	vector, pair := runGoSIMD(t, dir)
	if vector != pair || vector == "" {
		t.Errorf("vector %q, pair %q", vector, pair)
	}
}
