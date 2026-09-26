package codegen

// The -simd=<target> backend: v128 values carried in simd/archsimd
// 128-bit vector registers instead of [2]uint64 pairs.
//
// A target names the Go release whose archsimd API the helper set was
// generated against (tools/gen-simd-gosimd/gen.py); the API is
// experimental and not covered by the compatibility promise, so every
// generated file that depends on it is pinned to that release by build
// tag. Under any other Go (or without GOEXPERIMENT=simd, or on another
// GOARCH) the pair-carrier variant compiles instead, so the output
// always builds.
//
// Layout: a function whose body or signature touches a v128 is emitted
// twice — its usual pair-carrier form into <file>_nosimd.go under the
// negated tag, and its vector form into <file>_gosimd.go under the
// tag. Functions without v128 traffic are emitted once, untagged. The
// vector form types v128 as base.V128 (= archsimd.Uint64x2), lowers
// every SIMD op to a base.Simd_g_<op> helper (short inlinable method
// chains, see helpers/simd_g_<target>_<arch>.go), parks v128 literals in
// package-level [2]uint64 variables the compiler folds into memory
// operands, and skips the pair scalarization pass entirely.

import (
	"fmt"
	"go/ast"
	"go/token"
	"regexp"
	"sort"
	"strings"

	"github.com/goccy/wasm2go/internal/ssa"
	"github.com/goccy/wasm2go/internal/wasm"
)

const gosimdTargetGo127 = "go127"

// gosimdTags maps a target to the build constraint under which its
// helper set and the vector variant of every v128 function compile.
var gosimdTags = map[string]string{
	gosimdTargetGo127: "goexperiment.simd && go1.27 && !go1.28 && (amd64 || arm64)",
}

// gosimdOn reports whether Options.SIMD selected a target.
func (t *translator) gosimdOn() bool { return t.opts.SIMD != "" }

// gosimdTag is the build constraint of the vector variant files.
func (t *translator) gosimdTag() string { return gosimdTags[t.opts.SIMD] }

// gosimdNotTag is the build constraint of the pair variant files.
func (t *translator) gosimdNotTag() string { return "!(" + t.gosimdTag() + ")" }

// validateGoSIMD checks the option combination the backend supports.
func validateGoSIMD(opts Options, m *wasm.Module, multiPackage bool) error {
	if opts.SIMD == "" {
		return nil
	}
	if _, ok := gosimdTags[opts.SIMD]; !ok {
		return fmt.Errorf("wasm2go: Options.SIMD %q is not a known target (have: go127)", opts.SIMD)
	}
	if !opts.PureOnly {
		return fmt.Errorf("wasm2go: Options.SIMD requires Options.PureOnly (the archsimd variant replaces the asm bundle)")
	}
	if !multiPackage {
		return fmt.Errorf("wasm2go: Options.SIMD requires the multi-package layout (module above the multi-package threshold, or SetMultiPackageThreshold(0))")
	}
	if opts.OutlineMinValues > 0 {
		return fmt.Errorf("wasm2go: Options.SIMD does not support Options.OutlineMinValues (packed outline boundaries carry v128 as pairs)")
	}
	for i, g := range m.Globals {
		if g.Type.Type == wasm.ValV128 {
			return fmt.Errorf("wasm2go: Options.SIMD does not support v128 globals (global %d)", i)
		}
	}
	for _, e := range m.Exports {
		if e.Kind == wasm.ExportFunc && sigHasV128(m.FuncTypeOf(e.Index)) {
			return fmt.Errorf("wasm2go: Options.SIMD does not support v128 in an exported signature (export %q)", e.Name)
		}
	}
	var fi uint32
	for _, imp := range m.Imports {
		if imp.Kind != wasm.ImportFunc {
			continue
		}
		if sigHasV128(m.Types[imp.TypeIdx]) {
			return fmt.Errorf("wasm2go: Options.SIMD does not support v128 in an imported signature (%s.%s)", imp.Module, imp.Name)
		}
		fi++
	}
	return nil
}

// sigHasV128 reports whether a v128 crosses the function boundary.
func sigHasV128(ft wasm.FuncType) bool {
	for _, p := range ft.Params {
		if p == wasm.ValV128 {
			return true
		}
	}
	for _, r := range ft.Results {
		if r == wasm.ValV128 {
			return true
		}
	}
	return false
}

// v128Type is the Go type carrying a v128 in the body being emitted:
// base.V128 in the vector variant, [2]uint64 otherwise. Asking for it
// marks the current function as v128-touching, which is what selects
// it for the second, vector emission.
func (t *translator) v128Type() ast.Expr {
	t.fnUsesV128 = true
	if t.gosimd {
		return t.helperRef("V128")
	}
	return goTypeForSSAType(ssa.TypeV128)
}

// goTypeOf is goTypeOf with the v128 carrier of the current variant.
func (t *translator) goTypeOf(v wasm.ValType) ast.Expr {
	if v == wasm.ValV128 {
		return t.v128Type()
	}
	return goTypeOf(v)
}

// goTypeSSA is goTypeForSSAType with the v128 carrier of the current variant.
func (t *translator) goTypeSSA(typ ssa.Type) ast.Expr {
	if typ == ssa.TypeV128 {
		return t.v128Type()
	}
	return goTypeForSSAType(typ)
}

// goType is the emitter-side goTypeForSSAType; nil translators (unit
// tests) keep the pair carrier.
func (em *ssaEmitter) goType(typ ssa.Type) ast.Expr {
	if em.t == nil {
		return goTypeForSSAType(typ)
	}
	return em.t.goTypeSSA(typ)
}

// gosimd reports whether the emitter is producing the vector variant.
func (em *ssaEmitter) gosimd() bool { return em.t != nil && em.t.gosimd }

// simdKConst is one v128 literal of the function being emitted, parked
// in a package-level variable: `var F_foo__k0 = [2]uint64{lo, hi}`.
type simdKConst struct {
	name string
	val  [2]uint64
}

// simdConstRef returns `Simd_g_const(&<fn>__k<N>)` for val, registering
// the variable on first use. Names are per function (the outlined loops
// of a function share its table via curFuncName), so they cannot
// collide across the group files of one package.
func (t *translator) simdConstRef(val [2]uint64) ast.Expr {
	if t.simdKIdx == nil {
		t.simdKIdx = map[[2]uint64]int{}
	}
	i, ok := t.simdKIdx[val]
	if !ok {
		i = len(t.simdKs)
		t.simdKIdx[val] = i
		t.simdKs = append(t.simdKs, simdKConst{name: fmt.Sprintf("%s__k%d", t.curFuncName, i), val: val})
	}
	t.useHelper("simd_g_const")
	return &ast.CallExpr{Fun: t.helperRef("simd_g_const"), Args: []ast.Expr{
		&ast.UnaryExpr{Op: token.AND, X: newID(t.simdKs[i].name)},
	}}
}

// simdConstDecls returns the variable declarations registered by
// simdConstRef since the last call and resets the table.
func (t *translator) simdConstDecls() []ast.Decl {
	var out []ast.Decl
	for _, k := range t.simdKs {
		out = append(out, &ast.GenDecl{Tok: token.VAR, Specs: []ast.Spec{&ast.ValueSpec{
			Names: []*ast.Ident{newID(k.name)},
			Values: []ast.Expr{&ast.CompositeLit{
				Type: &ast.ArrayType{Len: intLit(2), Elt: newID("uint64")},
				Elts: []ast.Expr{
					&ast.BasicLit{Kind: token.INT, Value: fmt.Sprintf("0x%x", k.val[0])},
					&ast.BasicLit{Kind: token.INT, Value: fmt.Sprintf("0x%x", k.val[1])},
				},
			}},
		}}})
	}
	t.simdKs = nil
	t.simdKIdx = nil
	return out
}

// gosimdLaneOp reports whether a pure SIMD helper takes a lane
// immediate as its second argument (extract_lane / replace_lane).
func gosimdLaneOp(name string) bool {
	return strings.Contains(name, "_extract_lane") || strings.HasSuffix(name, "_replace_lane")
}

// gosimdName maps a pair-carrier helper name to its vector twin.
func gosimdName(name string) string { return "simd_g_" + strings.TrimPrefix(name, "simd_") }

// gosimdShufflePatterns normalizes an i8x16.shuffle immediate into the
// two index vectors simd_g_i8x16_shuffle2 takes: an entry in 0..15
// selects from its operand, 0x80 yields zero (VPSHUFB and TBL agree on
// that). onlyA / onlyB report that the other operand contributes no
// lane, so a single-source swizzle suffices.
func gosimdShufflePatterns(pat [2]uint64) (ia, ib [2]uint64, onlyA, onlyB bool) {
	onlyA, onlyB = true, true
	for i := 0; i < 16; i++ {
		idx := uint8(pat[i>>3] >> (8 * uint(i&7)))
		va, vb := uint8(0x80), uint8(0x80)
		switch {
		case idx < 16:
			va = idx
			onlyB = false
		case idx < 32:
			vb = idx - 16
			onlyA = false
		}
		ia[i>>3] |= uint64(va) << (8 * uint(i&7))
		ib[i>>3] |= uint64(vb) << (8 * uint(i&7))
	}
	return
}

// pureForDrop reports whether leaving v unemitted is unobservable: a
// hoisted value is emitted by its own statement anyway, and a literal,
// parameter or pure helper call over such values has no side effect.
func (em *ssaEmitter) pureForDrop(v *ssa.Value) bool {
	if em.curHoist[v.ID] {
		return true
	}
	switch v.Op {
	case ssa.OpSimdConst, ssa.OpParam, ssa.OpConst32, ssa.OpConst64:
		return true
	case ssa.OpSimdCall:
		for _, a := range v.Args {
			if !em.pureForDrop(a) {
				return false
			}
		}
		return true
	}
	return false
}

// emitGoSIMDCall lowers OpSimdCall in the vector variant: the
// simd_g_<op> helper, with a lane immediate folded into the name and a
// constant shuffle pattern folded into the emitter-normalized forms.
func (em *ssaEmitter) emitGoSIMDCall(v *ssa.Value, emit func(*ssa.Value) (ast.Expr, error)) (ast.Expr, error) {
	name, _ := v.Aux.(string)
	if name == "" {
		return nil, fmt.Errorf("ssa emit: OpSimdCall without name aux")
	}
	t := em.t
	args := v.Args
	call := func(gname string, exprs ...ast.Expr) ast.Expr {
		em.useHelper(gname)
		return &ast.CallExpr{Fun: em.helperRef(gname), Args: exprs}
	}
	if name == "simd_i8x16_shuffle" && len(args) == 3 && args[2].Op == ssa.OpSimdConst {
		pat, _ := args[2].Aux.([2]uint64)
		ia, ib, onlyA, onlyB := gosimdShufflePatterns(pat)
		switch {
		case onlyA && em.pureForDrop(args[1]):
			a, err := emit(args[0])
			if err != nil {
				return nil, err
			}
			return call("simd_g_i8x16_swizzle_c", a, t.simdConstRef(ia)), nil
		case onlyB && em.pureForDrop(args[0]):
			b, err := emit(args[1])
			if err != nil {
				return nil, err
			}
			return call("simd_g_i8x16_swizzle_c", b, t.simdConstRef(ib)), nil
		}
		a, err := emit(args[0])
		if err != nil {
			return nil, err
		}
		b, err := emit(args[1])
		if err != nil {
			return nil, err
		}
		return call("simd_g_i8x16_shuffle2", a, b, t.simdConstRef(ia), t.simdConstRef(ib)), nil
	}
	gname := gosimdName(name)
	if gosimdLaneOp(name) {
		if len(args) < 2 || args[1].Op != ssa.OpConst32 {
			return nil, fmt.Errorf("ssa emit: %s lane immediate is not a constant", name)
		}
		gname += fmt.Sprintf("_l%d", args[1].AuxInt)
		args = append([]*ssa.Value{args[0]}, args[2:]...)
	}
	var exprs []ast.Expr
	for _, a := range args {
		e, err := emit(a)
		if err != nil {
			return nil, err
		}
		exprs = append(exprs, e)
	}
	return call(gname, exprs...), nil
}

// emitGoSIMDMemCall lowers OpSimdMemCall in the vector variant:
// simd_g_<op>(m, addr, offset[, vec]) with a lane immediate folded into
// the name.
func (em *ssaEmitter) emitGoSIMDMemCall(v *ssa.Value, emit func(*ssa.Value) (ast.Expr, error)) (ast.Expr, error) {
	name, _ := v.Aux.(string)
	if name == "" {
		return nil, fmt.Errorf("ssa emit: OpSimdMemCall without name aux")
	}
	gname := gosimdName(name)
	args := v.Args
	if strings.Contains(name, "_lane") {
		if len(args) < 3 || args[2].Op != ssa.OpConst32 {
			return nil, fmt.Errorf("ssa emit: %s lane immediate is not a constant", name)
		}
		gname += fmt.Sprintf("_l%d", args[2].AuxInt)
		args = append(append([]*ssa.Value{}, args[:2]...), args[3:]...)
	}
	em.useHelper(gname)
	exprs := []ast.Expr{newID("m")}
	for _, a := range args {
		e, err := emit(a)
		if err != nil {
			return nil, err
		}
		exprs = append(exprs, e)
	}
	return &ast.CallExpr{Fun: em.helperRef(gname), Args: exprs}, nil
}

// gosimdFile finishes the multi-package spelling of a -simd helper file
// after appendSimdHelperFiles' generic simd_ → Simd_ rename: the
// name-filtered helpers it calls (simdEA, memBound, the OOB trap) and the
// Module field it reads are exported in base under emitHelpers'
// capitalization.
func (t *translator) gosimdFile(src []byte) []byte {
	if !t.multiPackage {
		return src
	}
	s := string(src)
	for _, r := range [][2]string{
		{"wasm_trap_Simd_oob(", "Wasm_trap_simd_oob("},
		{"simdEA(", "SimdEA("},
		{"simdEA64(", "SimdEA64("},
		{"memBound(", "MemBound("},
		{"m.memSize.", "m.MemSize."},
	} {
		s = strings.ReplaceAll(s, r[0], r[1])
	}
	return []byte(s)
}

var gosimdCallRE = regexp.MustCompile(`\b([A-Za-z_][A-Za-z0-9_]*)\(`)

// gosimdHelperDeps lists the name-filtered helpers (helpers.go) the
// -simd helper files call, so emitHelpers can request them.
func gosimdHelperDeps(helperNames map[string]bool) []string {
	seen := map[string]bool{}
	for _, src := range []string{simdGGo127Amd64Src, simdGGo127Arm64Src} {
		for _, m := range gosimdCallRE.FindAllStringSubmatch(src, -1) {
			if helperNames[m[1]] {
				seen[m[1]] = true
			}
		}
	}
	out := make([]string, 0, len(seen))
	for n := range seen {
		out = append(out, n)
	}
	sort.Strings(out)
	return out
}
