package codegen_test

import (
	"bytes"
	"strings"
	"testing"

	"github.com/goccy/wasm2go/internal/codegen"
	"github.com/goccy/wasm2go/internal/wasm"
)

// sleb encodes a signed LEB128 immediate, the form every i32.const in a
// hand-assembled body below needs.
func sleb(v int64) []byte {
	var out []byte
	for {
		b := byte(v & 0x7f)
		v >>= 7
		if (v == 0 && b&0x40 == 0) || (v == -1 && b&0x40 != 0) {
			return append(out, b)
		}
		out = append(out, b|0x80)
	}
}

func i32Const(v int64) []byte { return append([]byte{0x41}, sleb(v)...) }

// addrConstsModule is a module whose static data occupies
// [65536, 65552) and whose stack-pointer global puts the top of the
// static region at 100000, so the address window staticDataRange
// derives is [4096, 100000):
//
//	returns_addr   returns 65600  — inside the window
//	returns_small  returns 4000   — below it (an ordinary small integer)
//	returns_high   returns 200000 — above it (stack/heap, not static data)
//	loads_addr     reads *(70000+8) — a memory access, folded into _consts
func addrConstsModule() *wasm.Module {
	body := func(instrs ...[]byte) []byte {
		out := []byte{0x00} // no local declarations
		for _, in := range instrs {
			out = append(out, in...)
		}
		return append(out, 0x0b)
	}
	return &wasm.Module{
		Types:    []wasm.FuncType{{Results: []wasm.ValType{wasm.ValI32}}},
		Memories: []wasm.MemoryType{{Limits: wasm.Limits{Min: 2}}},
		Globals: []wasm.Global{{
			Type: wasm.GlobalType{Type: wasm.ValI32, Mutable: true},
			Init: append(i32Const(100000), 0x0b),
		}},
		Datas: []wasm.DataSegment{{
			Offset: append(i32Const(65536), 0x0b),
			Bytes:  []byte("a static string\x00"),
		}},
		Functions: []wasm.Function{
			{TypeIdx: 0, Body: body(i32Const(65600))},
			{TypeIdx: 0, Body: body(i32Const(4000))},
			{TypeIdx: 0, Body: body(i32Const(200000))},
			// i32.const 70000; i32.load align=2 offset=8
			{TypeIdx: 0, Body: body(i32Const(70000), []byte{0x28, 0x02, 0x08})},
		},
		Exports: []wasm.Export{
			{Name: "returns_addr", Kind: wasm.ExportFunc, Index: 0},
			{Name: "returns_small", Kind: wasm.ExportFunc, Index: 1},
			{Name: "returns_high", Kind: wasm.ExportFunc, Index: 2},
			{Name: "loads_addr", Kind: wasm.ExportFunc, Index: 3},
		},
	}
}

func translateAddrConsts(t *testing.T, opts codegen.Options) string {
	t.Helper()
	opts.Package = "addrmod"
	opts.OutputImportPath = "gentest/addrmod"
	var buf bytes.Buffer
	res, err := codegen.Translate(&buf, addrConstsModule(), opts)
	if err != nil {
		t.Fatalf("translate: %v", err)
	}
	var sb strings.Builder
	sb.Write(buf.Bytes())
	for _, data := range res.AuxFiles {
		sb.Write(data)
	}
	for _, data := range res.Files {
		sb.Write(data)
	}
	return sb.String()
}

// TestAddrConstsTable pins what Options.AddrConsts does to constants in
// expression position: an address inside the static-data window becomes
// a named constant declared once per file, everything outside it keeps
// its inline literal. The declaration is the whole point — a rebuilt
// wasm that only shifted its data then rewrites that one line instead of
// every body that carries an address.
func TestAddrConstsTable(t *testing.T) {
	src := translateAddrConsts(t, codegen.Options{AddrConsts: true})

	if !strings.Contains(src, "const _a0 = 65600") {
		t.Errorf("no address constant for the in-window address; got:\n%s", addrTableLine(src))
	}
	if !strings.Contains(src, "int32(_a0)") {
		t.Error("the in-window address does not read the constant")
	}
	if strings.Contains(src, "int32(65600)") {
		t.Error("the in-window address survives as an inline literal")
	}
	// Outside the window: a small integer and a stack/heap address are
	// not static data and must not pay for a table slot.
	for _, lit := range []string{"int32(4000)", "int32(200000)"} {
		if !strings.Contains(src, lit) {
			t.Errorf("%s was routed through a constant; only static-data addresses belong there", lit)
		}
	}
	// The load's constant base is folded into the access offset and
	// read from _consts (70000+8); giving it an address constant of its
	// own would leave a declaration nothing reads.
	if !strings.Contains(src, "_consts[") {
		t.Error("the constant-base load no longer routes through _consts")
	}
	if strings.Contains(src, "_a1") {
		t.Error("the memory access base took an address slot of its own")
	}
}

// TestAddrConstsOffByDefault: the table is opt-in, and without it every
// constant stays the inline literal it has always been.
func TestAddrConstsOffByDefault(t *testing.T) {
	src := translateAddrConsts(t, codegen.Options{})
	if strings.Contains(src, "_a0") {
		t.Error("an address constant appeared without Options.AddrConsts")
	}
	if !strings.Contains(src, "int32(65600)") {
		t.Error("the address lost its inline literal without Options.AddrConsts")
	}
}

// TestAddrConstsMaxOverride: AddrConstsMax replaces the derived top of
// the window, for a module whose layout the stack-pointer heuristic
// misreads.
func TestAddrConstsMaxOverride(t *testing.T) {
	src := translateAddrConsts(t, codegen.Options{AddrConsts: true, AddrConstsMax: 65600})
	// 65600 is now the exclusive top of the window, so it stays inline
	// and no table is emitted at all.
	if !strings.Contains(src, "int32(65600)") {
		t.Error("AddrConstsMax did not exclude the address at the window top")
	}
	if strings.Contains(src, "_a0") {
		t.Errorf("a constant was emitted although every address is outside the window: %s", addrTableLine(src))
	}
}

func addrTableLine(src string) string {
	for _, line := range strings.Split(src, "\n") {
		if strings.HasPrefix(line, "const _a") {
			if len(line) > 200 {
				return line[:200] + "..."
			}
			return line
		}
	}
	return "(no address constants)"
}
