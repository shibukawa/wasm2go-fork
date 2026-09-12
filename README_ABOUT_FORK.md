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
suffixes, no leading underscore).

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

### `-addr-consts`: static-data addresses out of the function bodies

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

`-addr-consts` names every constant that lands in the module's
static-data window and declares the names once per emitted file:

```go
const _a0, _a1, _a2 = 4442992, 322584, 471635   // one line, in p0.go

F_errmsg_internal(m, int32(_a1), int32(0))       // was int32(322584)
```

A named *constant*, not a table: `int32(_a1)` is still a constant
expression, so the compiler folds it into the instruction stream exactly
as it folded the literal and the generated machine code does not change
at all (measured below). Only the one declaration absorbs a layout shift.

- The window is `[lowest data-segment destination, top of .bss)`, floored
  at 4096 (below that a constant is far more likely a size or a mask than
  a pointer). `.bss` has no data segment, so its top is taken from the
  highest constant global initializer above the data segments — wasm-ld's
  `__stack_pointer` — capped by the declared initial memory.
  `-addr-consts-max` overrides it; the derived window is printed to
  stderr. Classifying loosely is safe: the constant holds the exact
  value, so a non-address only costs a name. Classifying narrowly only
  costs churn.
- Constants outside the window keep their inline literal. On a memory64
  module addresses are i64, so there the i32 constants stay inline and
  `_a64_<n>` carries the i64 ones.
- Memory-access offsets are untouched: a load or store with a constant
  base still folds base+offset into one `_consts` entry, which was
  already diff-stable (the whole table is one line) and which is a `var`
  on purpose — that one must NOT be foldable into an addressing immediate
  (the arm64 literal-pool hazard). The address constant such a base would
  otherwise get is deliberately not allocated.
- Names are handed out in first-use order, so a pure layout shift leaves
  every name where it was and rewrites the values alone. A function added
  in the middle of a chunk can still bump the names after it; on the
  citext commit that mechanism (the existing `_consts` table) moved 290
  body lines out of 55,809.

For pgmem's module: window `[4096, 12919808)`, 31,745 constants over the
six chunk packages, 81,636 reads. Simulating a pure data shift over the
generated tree (a string inserted early, every address above it +688,
`data.bin` grown to match):

| | `-symbol-names -group-files` | + `-addr-consts` |
|---|---|---|
| files changed | 1,429 | 7 |
| lines changed | 51,740 | 12 |
| thin pack `git push` would send | 1.44 MB | 131 KB |

Projecting the same substitution onto the real citext commit (which also
adds functions, so its residue is larger) takes it from 55,809 changed
lines to 5,912 plus the six declarations.

What it costs. Nothing at runtime: p0's compiled text section is
identical byte for byte with and without the flag (2,589,280 bytes), and
on the asm-bundle path the generated `arm64.s` is unchanged — the
constants are immediates there as before. The package archive grows 1.2%
(33.09 MB → 33.48 MB of `.a`) for the export data of 4,448 constant
names, which never reaches a linked binary. What it does cost is reading
the generated code: the bodies no longer show the addresses, and the
declaration at the top of `pN.go` is the place to look them up.

Verified by generating pgmem's module both ways and substituting the
declarations back into the bodies: all 1,796 files come out identical to
the `-addr-consts`-less output (1,791 byte for byte, the six `pN.go` and
the main file modulo the declaration's own line), and the whole 102 MB
tree compiles. Without the flag the output is byte for byte what it was.

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

### gcasm on Go 1.27

- Go 1.27 prints data symbols in `-S` listings as `... size=N align=0xM`;
  the capture regex now accepts the suffix (previously every jump table
  came back "not captured" and the bundle aborted).
- Jump-table shapes the transform does not recognise (for example a spill
  between the table `LEAQ` and the indirect `JMP`) fall back to the
  pure-Go body for that function only, instead of aborting the build.

## Testing

Most of the test suite compiles `testdata/*.wat` with `wat2wasm` at test
time and fails without it. The fork's own tests avoid that:

```
go test ./internal/wasm -run 'NameSection|CustomSection'
go test ./internal/codegen -run 'SymbolNames|SymbolFuncNames|Group|AddrConsts'
go test ./internal/gcasm -run Align
```

`testdata/symnames.wasm` and `symnames2.wasm` are prebuilt with
`wasm-tools parse` (which keeps `$names` as a name section, unlike
wat2wasm); regenerate them the same way after editing the `.wat` files.
