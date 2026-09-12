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
go test ./internal/codegen -run 'SymbolNames|SymbolFuncNames|Group'
go test ./internal/gcasm -run Align
```

`testdata/symnames.wasm` and `symnames2.wasm` are prebuilt with
`wasm-tools parse` (which keeps `$names` as a name section, unlike
wat2wasm); regenerate them the same way after editing the `.wat` files.
