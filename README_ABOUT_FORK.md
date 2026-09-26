# About this fork

This is [goccy/wasm2go](https://github.com/goccy/wasm2go) plus the changes
[pgmem](https://github.com/shibukawa/pgmem) needs to turn its PostgreSQL
wasm module into Go. Upstream's [README](README.md) still describes the
tool; this file covers only what differs.

## Layout and versioning

| branch / tag | meaning |
|---|---|
| `main` | mirrors upstream `main`; never carries fork commits |
| `pgmem` | the fork: upstream `main` plus the commits listed below |
| `vX.Y.Z` | upstream's own tags, carried over unchanged |
| `vX.Y.Z-fork.N` | a fork release: upstream `vX.Y.Z` plus the fork commits, N counting up per release |

The module path stays `github.com/goccy/wasm2go`, so the fork is consumed
by cloning and building, not by `go get`. pgmem pins it in
`wasm/wasm2go.lock` (repository, branch, commit) and builds
`cmd/wasm2go` from a clone at that commit.

The current base is upstream `v0.5.15` (commit 730057c, "gcasm: arm64 fhm
override feature level", #69), so the fork tags read `v0.5.15-fork.N`.
Releases 1 to 4 were first tagged `v0.5.9-fork.N`, from before upstream
tagged that commit; those names are retired and the same commits now
carry the `v0.5.15-fork.N` names.

To take a new upstream version: fast-forward `main` from
`https://github.com/goccy/wasm2go.git`, merge (or rebase) `pgmem` onto it,
run the test subset below, tag `vX.Y.Z-fork.1` for the new base, and bump
pgmem's lock file.

## Changes relative to upstream

### `-symbol-names` and `-chunks`: rebuild-stable output

Upstream names every generated function `Fn<wasm function index>` and
distributes them over the `p0`, `p1`, ... chunk packages by first-fit-
decreasing bin packing on body size. Both are unstable under small source
changes: one added import renumbers every defined function, and a body
that grows moves itself and its neighbours to another package. For a
102 MB generated tree that turned a one-line C change into a
200,000-line diff.

With `-symbol-names` the generator

- reads the wasm `name` custom section (new in `internal/wasm`,
  `Module.FuncNames`) and names each function `F_<symbol>`, mangled to a
  Go identifier (`f_<symbol>` in single-file mode). Functions without a
  name keep `Fn<index>`;
- numbers repeated names (C `static` functions from different files)
  `F_name_1`, `F_name_2`, ... in index order, and removes the suffix
  Binaryen appends for the same purpose (`heap_getattr_1883`, a
  pre-optimization index that shifts as well). That suffix is recognised
  when the bare name is also a function, or when the number exceeds the
  function's own index; a genuine trailing number larger than the index
  (`conv_utf8_to_18030`) is dropped too, leaving a shorter but still
  unique name;
- assigns each function to chunk `fnv32a(name) mod N`, with N given by
  `-chunks` or derived from the module size. Pin `-chunks` so growth
  never reshuffles the packages;
- suffixes a colliding export method name with the export name instead
  of the function index.

Requirements: `-pure` (the asm bundle derives `Fn<index>` symbols itself,
so `-symbol-names` is rejected without it), and a wasm that still has its
name section (emscripten `--profiling-funcs`, or no `--strip-all` with
wasm-ld).

Measured on pgmem's module (11,177 functions, 5.9 M generated lines) for
one added import plus one added function:

| naming | files changed | lines changed |
|---|---|---|
| `Fn<index>` | 21 | 394,052 |
| `-symbol-names -chunks 6` | 4 | 10 |

Usage:

```
wasm2go -pure -symbol-names -chunks 6 -i module.wasm -out-dir gen \
  -pkg gen -import example.com/proj/gen
```

### `-group-files`: one file per subject instead of one per package

`-symbol-names` alone still writes each chunk package as a single
`pN.go` of 13-21 MB, which diff viewers refuse to render and editors
struggle with. `-group-files` spreads the function bodies of a package
over files named after what the functions operate on; `pN.go` keeps the
constant table and the element-segment initializers.

The group of a function is the first token of its symbol name that is
not a verb — `get_relation_info` and `RelationGetRelid` both file under
`relation.go` — lower-cased; a single-letter token is glued to its
successor (`x_log`). Tokens come from underscores, or from camel-case
boundaries when the name has none. The built-in verb list (get, set,
new, free, is, has, check, make, ...) can be replaced with
`-group-verbs a,b,c` (`NONE` skips nothing).

Sizes are judged over the whole module so a group has the same file
name in every package it touches:

- a group with fewer than `-group-min` functions (8) goes to the misc
  pool, `misc_<leading letters of the name>.go`, deepened one letter at
  a time until each file fits;
- a group whose source per package exceeds `-group-max-bytes` (512 KiB)
  is split by its next token (`pg_stat.go`, `pg_finfo.go`; sub-groups
  below the minimum stay with the parent), then by leading letters;
- a single function above `-group-huge-bytes` (128 KiB) gets
  `<group>_<name>.go` to itself.

Every split is derived from names — there is no hashing or numbering —
so a rebuilt module keeps every file it does not touch; only a group
that crosses a threshold changes shape. File names are sanitized so the
go tool reads them as plain package source (no `_test`, GOOS or GOARCH
suffixes, no leading underscore), and a Windows device name gets a
trailing underscore (`aux_.go`): whatever the extension, Git for Windows
refuses to check out `aux.go`, and the go command refuses it in a module
zip. `lpt0` counts as one too; Windows has no such device, but Git for
Windows refuses it all the same.

Measured on pgmem's history (five commits, two of which rebuild the
wasm: one adds a host import, one links in pgcrypto). The table is the
size of the thin pack `git push` would send for each commit, i.e. the
new objects delta-compressed against the previous commit:

| commit | `Fn<index>`, one file per package | `-symbol-names`, one file per package | `-symbol-names -group-files` |
|---|---|---|---|
| initial import | 19.2 MB | 20.3 MB | 20.2 MB |
| one host import added | 8.4 MB | 0.3 MB | 0.3 MB |
| two commits without a wasm rebuild | 0.1 MB | 0.1 MB | 0.1 MB |
| pgcrypto linked in (+242 functions, data addresses shift) | 4.5 MB | 6.9 MB | 2.5 MB |
| total | 32.4 MB | 27.7 MB | 23.2 MB |
| files under the generated tree | 25 | 25 | 1,825 |

Symbol names alone fix the small change (a renumbering that touched
394,052 lines becomes 10). The large change gets cheaper only once the
files are small: with 13-21 MB files, changes scattered through a file
delta-compress poorly (git's delta search has a bounded window), and the
symbol-named single-file layout was actually worse than the index-named
one there. Per-subject files bring it down to 2.5 MB, and a working-tree
edit rewrites one file of mostly under 100 KB instead of a 20 MB one.
The initial import is the generated code itself and does not change.

Usage (with the same `-pure -symbol-names` prerequisites):

```
wasm2go -pure -symbol-names -chunks 6 -group-files -i module.wasm \
  -out-dir gen -pkg gen -import example.com/proj/gen
```

### `-addr-consts`: static-data addresses as named constants

`-symbol-names` and `-group-files` keep the *shape* of the output stable.
What they cannot keep stable is an address: adding one string literal to
one C file makes wasm-ld shift every static-data address above it, and
those addresses sit inline in the body of every function that passes a
pointer — a format string to `errmsg`, a global's address to anything.

Measured on pgmem's `bundle citext` commit (one small extension linked
in, generated tree only):

| | |
|---|---|
| files changed | 1,426 `.go` + `data.bin` |
| lines | +55,809 / −50,361 |
| added lines that differ from a deleted line *only* in an integer ≥ 4096 | 49,835 (89%) |
| lines that are a real change | 5,760 |
| what `data.bin` itself cost (thin pack) | 102 KB |

So the blob is not the problem — git deltas it — and neither is the
layout of the tree. The problem is 50,000 lines of shifted literals.
The insertion is not at the end, either: the new string landed 1.3% into
the blob (a sorted symbol table), so linking the extension last would not
have helped.

`-addr-consts` gives every constant that lands in the module's
static-data window a name, declared once per emitted file:

```go
const _a_F_errfinish_0, _a_F_errfinish_1 = 471635, 86458   // one line, p0.go

F_errmsg_internal(m, int32(_a_F_errfinish_0), int32(0))     // was int32(471635)
```

Named *constants*, not a table: `int32(_a_F_errfinish_0)` is still a
constant expression, so the compiler folds it into the instruction stream
exactly as it folded the literal.

**The name is keyed by the use site** — the function plus the ordinal of
the value within that function — and deliberately not by the value.
Value keying dedupes better and was tried first; it does not survive a
rebuild. A shifted data layout moves objects by *different* amounts, so
two values that used to coincide stop coinciding (or start), the file's
slot sequence gains or loses an entry, and every name after that point
changes. On pgmem's `bundle auto_explain` commit that renumbering was
31,494 of the 56,831 changed lines. Keyed by the use site, a function's
names depend on nothing but its own body.

The memory-access offsets move the same way, so under `-addr-consts`
they leave the file-wide, value-keyed `_consts` table for one array per
function:

```go
var (
	_c_F_errfinish = [3]uintptr{4442992, 4437600, 4062048}
)
```

Still a `var`: the arm64 literal-pool hazard that table exists for (see
`largeConstThreshold`) requires that an access offset NOT be foldable
into an addressing immediate. Without `-addr-consts` the old `_consts`
table is emitted unchanged.

- The window is `[lowest data-segment destination, top of .bss)`, floored
  at 4096 (below that a constant is far more likely a size or a mask than
  a pointer). `.bss` has no data segment, so its top is taken from the
  highest constant global initializer above the data segments — wasm-ld's
  `__stack_pointer` — capped by the declared initial memory.
  `-addr-consts-max` overrides it; the derived window is printed to
  stderr. Classifying loosely is safe: the constant holds the exact
  value, so a non-address only costs a name.
- Constants outside the window keep their inline literal. On a memory64
  module addresses are i64, so there the i32 constants stay inline and
  `_a64_<func>_<n>` carries the i64 ones.
- A constant base that the access path folds into the offset gets no
  address constant of its own, so no declaration goes unread.

Measured on pgmem's module: window `[4096, 13136384)`, 48,118 constants
in 7,884 functions over the six chunk packages. For the `bundle
auto_explain` commit (a rebuild that both shifts every address and adds a
whole extension):

| | `-symbol-names -group-files` | + `-addr-consts`, value-keyed | + use-site-keyed |
|---|---|---|---|
| files changed | 1,563 | 1,007 | 37 |
| lines changed | 84,502 | 56,827 | 25,535 |

Of those last 25,535 lines, 20,568 are auto_explain's own new code, 4,444
are one changed `_c_<func>` line per function, 6 are the address
declarations, and 517 are bodies whose own constant set changed.

What it costs. Almost nothing at runtime: p0's compiled text section came
out 816 bytes *smaller* (2,930,576 vs 2,931,392) because the addresses
stay immediates, while its data grew 15 KB for the per-function offset
arrays, which no longer dedupe across the package. What it does cost is
reading the generated code: the bodies no longer show the addresses, and
the declaration at the top of `pN.go` is where to look them up.

Verified by generating pgmem's module both ways and substituting the
declarations back into the bodies: every file comes out identical to the
`-addr-consts`-less output, and the whole 102 MB tree compiles and passes
pgmem's suite. Without the flag the output is byte for byte what it was.

Usage:

```
wasm2go -pure -symbol-names -chunks 6 -group-files -addr-consts \
  -i module.wasm -out-dir gen -pkg gen -import example.com/proj/gen
```

### Export method name collisions

Two exports can mangle to the same Go method (`relation_close` and
`RelationClose`). Upstream failed; the fork keeps both, suffixing the
later one (`RelationClose_relation_close`).

### `InitData` for caller-provided memories

`NewWithMemory` deliberately skips the data-segment copy so a snapshot
can be restored into the memory. The fork also emits `InitData(m)` so a
fresh module can be booted in memory the caller allocated (pgmem maps
linear memory outside the Go heap).

### Bounds of caller-provided memories

pgmem maps the whole growable range up front and places its shared
segments above the size the guest sees. The bounds-checked accesses of
a wasm32 memory (`memory.fill`, `memory.copy`, atomics, and the `v128`
loads and stores of the pure-Go and `-simd` helpers) therefore stop at
the end of the slice, as the unchecked scalar loads and stores do
(`memBound`). A memory declared shared stays bounded by the guest-visible
size, because its slice always spans the declared maximum. memory64
modules and the SIMD splices of the asm backend still check against the
guest-visible size.

### gcasm on Go 1.27

- Go 1.27 prints data symbols in `-S` listings as `... size=N align=0xM`;
  the capture regex now accepts the suffix (previously every jump table
  came back "not captured" and the bundle aborted).
- Jump-table shapes the transform does not recognise (for example a spill
  between the table `LEAQ` and the indirect `JMP`) fall back to the
  pure-Go body for that function only, instead of aborting the build.

### `-simd=go127`: v128 code over `simd/archsimd`

The pair backends carry a wasm `v128` as two `uint64` (or a `[2]uint64`)
and run every SIMD op as a call into an asm or scalar helper, so a
vector value visits the general-purpose registers between any two ops.
Go 1.27's `GOEXPERIMENT=simd` adds `simd/archsimd`, whose 128-bit vector
types the compiler keeps in vector registers and passes through the
register ABI. `-simd=go127` targets it:

```
wasm2go -pure -symbol-names -chunks 1 -group-files -simd=go127 \
  -out-dir gen -pkg gen -import example.com/proj/gen
```

- Every function that touches a `v128` (in its body, its signature, or
  an indirect-call type) is emitted twice. Its usual pair form goes to
  `<file>_nosimd.go`, a second form over `base.V128`
  (`= archsimd.Uint64x2`) to `<file>_gosimd.go`. The two are selected by
  the build tag

  ```
  goexperiment.simd && go1.27 && !go1.28 && (amd64 || arm64)
  ```

  and its negation, so `GOEXPERIMENT=simd go build` on Go 1.27 runs the
  vector code and every other build (no experiment, another release,
  another GOARCH) the pair code, from the same tree. Functions without
  `v128` traffic are emitted once, untagged, so the file set of a module
  that uses no SIMD is unchanged.
- In the vector form every SIMD op is a `base.Simd_g_<op>` call: a short
  method chain over `archsimd` that inlines, so consecutive ops stay in
  registers. `v128` literals become package-level `[2]uint64` variables
  (`F_foo__k0`) the compiler folds into memory operands; lane immediates
  are folded into the helper name (`Simd_g_i8x16_extract_lane_s_l3`);
  a constant `i8x16.shuffle` becomes one or two `VPSHUFB`/`TBL` over
  pre-normalized index vectors.
- The helper sets are `base/simd_g_go127_amd64.go` (AVX/AVX2) and
  `base/simd_g_go127_arm64.go` (NEON), generated by
  `tools/gen-simd-gosimd/gen.py` from the reference signatures in
  `internal/codegen/helpers`. Ops the generator has no native mapping
  for bridge to the existing helper through memory (correct, not fast);
  Go 1.27 leaves 10 such ops on amd64 and 4 on arm64, all rare
  (`f64x2` conversions, `f16x4`, `i32x4.trunc_sat_f32x4_u`).
  `internal/codegen/helpers/simd_g_go127_matrix_test.go` checks every
  helper against the pair-carrier reference under `GOEXPERIMENT=simd`.
- The target is explicit because `simd/archsimd` is experimental and not
  under the Go 1 compatibility promise (its API moved between 1.26 and
  1.27, and the 1.26 package was amd64-only). Nothing in the generated
  module code names `archsimd`: adding a release means a new target in
  `gen.py` (templates adjusted to that API, a new tag) and its name in
  `gosimdTags`, and regenerated modules keep building on every other
  release meanwhile.
- Requirements: `-pure` and the multi-package layout (the CLI switches
  to it for any module size when `-simd` is given, so `-out-dir` is
  required); a `v128` may not cross an export, an import or a global;
  `-outline` is not combined with it. On amd64 the vector variant needs AVX2 at run time (the
  `archsimd` 128-bit ops are VEX-encoded and the broadcasts are AVX2);
  `base` checks that in an `init` and panics with a message naming the
  fix, rather than faulting inside a function.
- Go 1.27 caveat on AVX-512 machines: the runtime's asynchronous
  preemption restores the vector registers with `VMOVDQU64 Z0..Z31` and
  no `VZEROUPPER`, so after the first preemption the upper halves of
  every register are dirty, and gc still moves vector values with the
  legacy-SSE `MOVUPS` (zero values, spills and reloads). Each such
  instruction then costs an SSE/AVX transition (~300 cycles measured
  on a Xeon): a per-pixel kernel of libwebp ran 30x slower until its
  zero vector was taken from memory instead of the zero value. The
  helper set avoids zero values for that reason; gc's spill code is
  out of its hands. Until the runtime is fixed, run such binaries with
  `GODEBUG=asyncpreemptoff=1` (or `//go:debug asyncpreemptoff=1` in the
  main package) when the CPU has AVX-512.

## Testing

Most of the test suite compiles `testdata/*.wat` with `wat2wasm` at test
time and fails without it. The fork's own tests avoid that:

```
go test ./internal/wasm -run 'NameSection|CustomSection'
go test ./internal/codegen -run 'SymbolNames|SymbolFuncNames|Group|AddrConsts'
go test ./internal/gcasm -run Align
GOEXPERIMENT=simd go test ./internal/codegen/helpers -run GoSIMD   # Go 1.27, amd64/arm64
```

`go test ./transpile -run GoSIMDDifferential` builds and runs a SIMD
module both ways (needs `wat2wasm` and a Go 1.27 toolchain).

`testdata/symnames.wasm` and `symnames2.wasm` are prebuilt with
`wasm-tools parse` (which keeps `$names` as a name section, unlike
wat2wasm); regenerate them the same way after editing the `.wat` files.
