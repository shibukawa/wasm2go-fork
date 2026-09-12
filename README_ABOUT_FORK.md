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
| `vX.Y.Z-fork.N` | a fork release: upstream `vX.Y.Z` (or later, see below) plus the fork commits, N counting up per release |

The module path stays `github.com/goccy/wasm2go`, so the fork is consumed
by cloning and building, not by `go get`. pgmem pins it in
`wasm/wasm2go.lock` (repository, branch, commit) and builds
`cmd/wasm2go` from a clone at that commit.

The current base is upstream commit 730057c ("gcasm: arm64 fhm override
feature level", #69), 11 commits past `v0.5.9`; upstream had not tagged
that range, so the fork tags read `v0.5.9-fork.N`.

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
go test ./internal/codegen -run 'SymbolNames|SymbolFuncNames'
go test ./internal/gcasm -run Align
```

`testdata/symnames.wasm` and `symnames2.wasm` are prebuilt with
`wasm-tools parse` (which keeps `$names` as a name section, unlike
wat2wasm); regenerate them the same way after editing the `.wat` files.
