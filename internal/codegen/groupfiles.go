package codegen

import (
	"sort"
	"strings"
	"unicode"
)

// File grouping for the symbol-named multi-package layout
// (Options.GroupFiles). A chunk package's function bodies are spread
// over files named after what the functions operate on: heap.go,
// relation.go, pg_stat.go. The key is the first token of the symbol
// name that is not a verb (get_relation_info → relation), a single
// letter glued to its successor (x_foo → x_foo), lower-cased. Groups
// below GroupMin functions share the misc_* files, split by the
// leading letters of their key; a group whose source exceeds
// GroupMaxBytes is split by its next token (pg_stat.go), then by
// letters; a function above GroupHugeBytes gets <group>_<name>.go
// alone. Every split is derived from names, so a rebuilt module keeps
// the files it does not touch.

// defaultGroupVerbs are the leading tokens skipped when choosing a
// group: the module's most frequent verbs plus the usual suspects.
// Options.GroupVerbs replaces the list.
var defaultGroupVerbs = []string{
	"get", "set", "new", "free", "is", "has", "check", "make", "find", "add",
	"assign", "init", "create", "build", "remove", "drop", "reset", "fetch",
	"count", "transform", "alloc", "release", "mark", "clear", "do", "try",
	"can", "put", "push", "pop", "read", "write", "load", "store", "copy",
	"compare", "cmp", "parse", "print", "show", "format", "dump", "convert",
	"validate", "verify", "apply", "process", "handle", "register", "lookup",
	"search", "insert", "delete", "update", "open", "close", "start", "end",
	"begin", "finish", "run", "fill", "flush", "emit", "append", "extract",
	"expand", "compute", "calc", "estimate", "generate", "gen", "prepare",
	"setup", "cleanup", "destroy", "reserve", "ensure", "report", "record",
	"send", "recv", "receive", "scan", "walk", "visit", "next", "skip",
	"advance", "sort", "merge", "split", "pack", "unpack", "encode", "decode",
	"serialize", "deserialize", "match", "eval", "evaluate", "test", "want",
	"need", "should", "equal", "equals", "fix", "fixup", "adjust", "resolve",
	"select", "choose", "pick", "collect", "gather", "accumulate", "commit",
	"abort", "rollback", "restore", "save", "recover", "replace", "rewrite",
	"substitute", "strip", "trim", "enforce", "deconstruct", "construct",
	"use", "wait", "notify", "signal", "lock", "unlock", "acquire", "pin",
	"unpin", "attach", "detach", "map", "unmap", "bind", "unbind", "enable",
	"disable", "activate", "deactivate", "error", "warn", "raise", "throw",
	"cast", "coerce", "simplify", "optimize", "preprocess", "postprocess",
	"plan", "rebuild", "recheck", "revalidate", "cost", "flatten", "pull",
	"swap", "exec", "execute", "call", "invoke", "dispatch", "emit", "assert",
	"require", "expect", "grow", "shrink", "extend", "truncate", "reserve",
	"allocate", "deallocate", "install", "uninstall", "define", "undefine",
	"declare", "compile", "link", "resolve", "finalize", "initialize", "reinit",
	"reinitialize", "refresh", "reload", "recompute", "recalc", "recalculate",
}

const (
	defaultGroupMin       = 8
	defaultGroupMaxBytes  = 512 << 10
	defaultGroupHugeBytes = 128 << 10
)

// groupNameTokens splits a symbol name into lower-cased tokens: on
// underscores when it has any, otherwise on camel-case boundaries
// (RelationGetRelid → relation, get, relid; XMLParse → xml, parse).
func groupNameTokens(name string) []string {
	name = strings.TrimLeft(name, "_")
	if name == "" {
		return nil
	}
	var toks []string
	if strings.Contains(name, "_") {
		for _, t := range strings.Split(name, "_") {
			if t != "" {
				toks = append(toks, strings.ToLower(t))
			}
		}
		return toks
	}
	rs := []rune(name)
	start := 0
	for i := 1; i < len(rs); i++ {
		prevUpper := unicode.IsUpper(rs[i-1])
		curUpper := unicode.IsUpper(rs[i])
		nextLower := i+1 < len(rs) && unicode.IsLower(rs[i+1])
		// boundary: lower/digit → Upper, or Upper Upper lower (end of
		// an acronym)
		if curUpper && (!prevUpper || nextLower) && i > start {
			toks = append(toks, strings.ToLower(string(rs[start:i])))
			start = i
		}
	}
	toks = append(toks, strings.ToLower(string(rs[start:])))
	return toks
}

// groupKey picks the group of a symbol: the first token that is not a
// verb; a single-letter token (x_, r_) is glued to the one after it,
// while two-letter ones (pg_, bt_, ts_) are subsystems of their own.
// The returned rest holds the tokens after the key token with verbs
// removed, for splitting oversized groups the same way. Nameless
// functions (Fn<index>) group under "fn".
func groupKey(name string, verbs map[string]bool) (key string, rest []string) {
	toks := groupNameTokens(name)
	if len(toks) == 0 {
		return "fn", nil
	}
	i := 0
	for i < len(toks)-1 && verbs[toks[i]] {
		i++
	}
	if len(toks[i]) == 1 && i < len(toks)-1 {
		key = toks[i] + "_" + toks[i+1]
		i += 2
	} else {
		key = toks[i]
		i++
	}
	for _, tok := range toks[i:] {
		if !verbs[tok] {
			rest = append(rest, tok)
		}
	}
	return key, rest
}

// groupFunc is one function to place: its generated name (sort key),
// the symbol its group derives from, and the size of its rendered
// source.
type groupFunc struct {
	name   string
	symbol string
	size   int
}

// groupFiles assigns every function of the module to a file stem (no
// directory, no .go). It runs over all packages at once so a group's
// size is judged globally and its members share one file name in
// every package they land in; the caller partitions the result by
// package. Sizes are judged against maxBytes as a module-wide total
// (the caller scales the per-file limit by the package count), and a
// single function above hugeBytes gets a file of its own. Every
// split is derived from names — next token, then leading letters —
// so a rebuild keeps the files it does not touch.
func groupFiles(funcs []groupFunc, verbs map[string]bool, min, maxBytes, hugeBytes int) map[string][]groupFunc {
	if min <= 0 {
		min = defaultGroupMin
	}
	if maxBytes <= 0 {
		maxBytes = defaultGroupMaxBytes
	}
	if hugeBytes <= 0 {
		hugeBytes = defaultGroupHugeBytes
	}
	type entry struct {
		f    groupFunc
		rest []string // tokens after the key, for token splits
		disc string   // token to refine by letters when tokens run out
	}
	out := map[string][]groupFunc{}
	emit := func(stem string, es []entry) {
		for _, e := range es {
			out[stem] = append(out[stem], e.f)
		}
	}
	total := func(es []entry) int {
		n := 0
		for _, e := range es {
			n += e.f.size
		}
		return n
	}
	// refine splits es by the first k letters of each entry's disc,
	// deepening where a part is still too big. An entry whose disc is
	// exhausted goes to the file named by its whole disc, and so does
	// a part left with a single entry; a level that would cut right
	// after an underscore is skipped.
	var refine func(stem string, es []entry, k int)
	refine = func(stem string, es []entry, k int) {
		parts := map[string][]entry{}
		for _, e := range es {
			d := e.disc
			if len(d) <= k {
				emit(stem+"_"+d, []entry{e})
				continue
			}
			parts[d[:k]] = append(parts[d[:k]], e)
		}
		for prefix, pes := range parts {
			switch {
			case len(pes) == 1:
				emit(stem+"_"+pes[0].disc, pes)
			case strings.HasSuffix(prefix, "_"):
				refine(stem, pes, k+1)
			case total(pes) <= maxBytes:
				emit(stem+"_"+prefix, pes)
			default:
				refine(stem, pes, k+1)
			}
		}
	}
	// place emits a group: huge members alone, the rest together when
	// they fit, otherwise split by the next token (sub-groups too
	// small for a file of their own stay with the parent) and finally
	// by letters.
	var place func(stem string, es []entry)
	place = func(stem string, es []entry) {
		var kept []entry
		for _, e := range es {
			if e.f.size > hugeBytes {
				emit(stem+"_"+strings.ToLower(strings.TrimLeft(e.f.symbol, "_")), []entry{e})
				continue
			}
			kept = append(kept, e)
		}
		es = kept
		if total(es) <= maxBytes || len(es) < 2 {
			emit(stem, es)
			return
		}
		sub := map[string][]entry{}
		var remainder []entry
		for _, e := range es {
			if len(e.rest) == 0 {
				remainder = append(remainder, e)
				continue
			}
			sub[e.rest[0]] = append(sub[e.rest[0]], entry{e.f, e.rest[1:], e.rest[0]})
		}
		for tok, ses := range sub {
			if len(ses) < min {
				remainder = append(remainder, ses...)
				continue
			}
			place(stem+"_"+tok, ses)
		}
		if total(remainder) <= maxBytes || len(remainder) < 2 {
			emit(stem, remainder)
			return
		}
		refine(stem, remainder, 1)
	}
	groups := map[string][]entry{}
	var misc []entry
	for _, f := range funcs {
		k, rest := groupKey(f.symbol, verbs)
		groups[k] = append(groups[k], entry{f, rest, k})
	}
	for k, es := range groups {
		if len(es) < min {
			// Too small for a file of its own: the misc pool, split
			// by the leading letters of the whole symbol name.
			for _, e := range es {
				misc = append(misc, entry{e.f, nil, strings.ToLower(strings.TrimLeft(e.f.symbol, "_"))})
			}
			continue
		}
		place(k, es)
	}
	if len(misc) > 0 {
		var kept []entry
		for _, e := range misc {
			if e.f.size > hugeBytes {
				emit("misc_"+strings.ToLower(strings.TrimLeft(e.f.symbol, "_")), []entry{e})
				continue
			}
			kept = append(kept, e)
		}
		refine("misc", kept, 1)
	}
	for stem := range out {
		sort.Slice(out[stem], func(a, b int) bool { return out[stem][a].name < out[stem][b].name })
	}
	return out
}

// goosGoarch lists the suffixes the go tool treats as build
// constraints in file names; a group file must not end in one.
var goosGoarch = map[string]bool{
	"aix": true, "android": true, "darwin": true, "dragonfly": true, "freebsd": true,
	"hurd": true, "illumos": true, "ios": true, "js": true, "linux": true, "nacl": true,
	"netbsd": true, "openbsd": true, "plan9": true, "solaris": true, "wasip1": true,
	"windows": true, "zos": true,
	"386": true, "amd64": true, "amd64p32": true, "arm": true, "armbe": true, "arm64": true,
	"arm64be": true, "loong64": true, "mips": true, "mipsle": true, "mips64": true,
	"mips64le": true, "mips64p32": true, "mips64p32le": true, "ppc": true, "ppc64": true,
	"ppc64le": true, "riscv": true, "riscv64": true, "s390": true, "s390x": true,
	"sparc": true, "sparc64": true, "wasm": true,
	"test": true,
}

// windowsDevices are the names Windows refuses as a file's base name
// whatever the extension (aux.go cannot be checked out there).
var windowsDevices = map[string]bool{
	"con": true, "prn": true, "aux": true, "nul": true,
	"com1": true, "com2": true, "com3": true, "com4": true, "com5": true,
	"com6": true, "com7": true, "com8": true, "com9": true,
	"lpt1": true, "lpt2": true, "lpt3": true, "lpt4": true, "lpt5": true,
	"lpt6": true, "lpt7": true, "lpt8": true, "lpt9": true,
}

// groupFileName turns a stem into a file name the go tool reads as
// plain package source: only [a-z0-9_], not starting with '_' (such
// files are ignored), no GOOS/GOARCH/_test suffix, not one of the
// names the layout already uses, and not a Windows device name.
func groupFileName(stem string, reserved map[string]bool) string {
	var b strings.Builder
	for _, r := range stem {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9', r == '_':
			b.WriteRune(r)
		case r >= 'A' && r <= 'Z':
			b.WriteRune(unicode.ToLower(r))
		default:
			b.WriteByte('_')
		}
	}
	s := strings.TrimLeft(b.String(), "_")
	if s == "" {
		s = "x"
	}
	parts := strings.Split(s, "_")
	for len(parts) > 1 && goosGoarch[parts[len(parts)-1]] {
		s += "_"
		parts = parts[:len(parts)-1]
		if len(parts) > 1 && goosGoarch[parts[len(parts)-1]] {
			continue
		}
		break
	}
	if reserved[s] || windowsDevices[s] {
		s += "_"
	}
	return s + ".go"
}
