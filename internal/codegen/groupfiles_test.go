package codegen

import (
	"fmt"
	"reflect"
	"sort"
	"testing"
)

func TestGroupNameTokens(t *testing.T) {
	cases := map[string][]string{
		"heap_insert":        {"heap", "insert"},
		"_bt_insert":         {"bt", "insert"},
		"RelationGetRelid":   {"relation", "get", "relid"},
		"XMLParse":           {"xml", "parse"},
		"ExecInitNode":       {"exec", "init", "node"},
		"cleanup":            {"cleanup"},
		"pg_stat_get_xact_1": {"pg", "stat", "get", "xact", "1"},
	}
	for name, want := range cases {
		if got := groupNameTokens(name); !reflect.DeepEqual(got, want) {
			t.Errorf("%s: got %v, want %v", name, got, want)
		}
	}
}

func TestGroupKey(t *testing.T) {
	verbs := map[string]bool{"get": true, "set": true, "is": true}
	cases := map[string][2]string{
		"heap_insert":        {"heap", "insert"},
		"get_relation_info":  {"relation", "info"},
		"get_set_x":          {"x", ""}, // all verbs: the last token stands
		"IsTransactionBlock": {"transaction", "block"},
		"x_foo_bar":          {"x_foo", "bar"},
		"pg_get_viewdef":     {"pg", "viewdef"},
		"Fn123":              {"fn123", ""},
		"":                   {"fn", ""},
		"get_x":              {"x", ""},
	}
	for name, want := range cases {
		key, rest := groupKey(name, verbs)
		restStr := ""
		for i, r := range rest {
			if i > 0 {
				restStr += " "
			}
			restStr += r
		}
		if key != want[0] || restStr != want[1] {
			t.Errorf("%s: got (%q, %q), want (%q, %q)", name, key, restStr, want[0], want[1])
		}
	}
}

func TestGroupFilesRules(t *testing.T) {
	var funcs []groupFunc
	add := func(sym string, size int) {
		funcs = append(funcs, groupFunc{name: "F_" + sym, symbol: sym, size: size})
	}
	// 3 heap functions: below min → misc pool, split by letter
	add("heap_insert", 10)
	add("heap_delete", 10)
	add("heap_update", 10)
	add("hash_x", 10) // another h: same misc file as long as it fits
	// 5 relation functions incl. a verb-led one: their own file
	for _, s := range []string{"relation_open", "relation_close", "get_relation_info", "RelationGetRelid", "relation_x"} {
		add(s, 10)
	}
	// pg: 20 stat + 20 finfo + 3 others at 10 bytes each = 430 > max 250
	// → split by next token; the 3 leftovers stay in pg
	for i := 0; i < 20; i++ {
		add(fmt.Sprintf("pg_stat_%d", i), 10)
		add(fmt.Sprintf("pg_finfo_%d", i), 10)
	}
	add("pg_a", 10)
	add("pg_b", 10)
	add("pg_c", 10)
	// out: one huge node printer plus small ones that fit
	add("_outNode", 1000)
	for i := 0; i < 8; i++ {
		add(fmt.Sprintf("_outPlan%d", i), 10)
	}
	// jsonb_<i>: no sub-group reaches min, too big together → letters
	// of the rest token
	for i := 0; i < 25; i++ {
		add(fmt.Sprintf("jsonb_%c%d", 'a'+i%3, i), 20)
	}
	verbs := map[string]bool{"get": true}
	got := groupFiles(funcs, verbs, 4, 250, 500)
	sizes := map[string]int{}
	for stem, fs := range got {
		sizes[stem] = len(fs)
	}
	want := map[string]int{
		"misc_h": 4, "relation": 5, "pg": 3, "pg_stat": 20, "pg_finfo": 20,
		"out_outnode": 1, "out": 8,
		"jsonb_a": 9, "jsonb_b": 8, "jsonb_c": 8,
	}
	for stem, n := range want {
		if sizes[stem] != n {
			t.Errorf("%s: %d functions, want %d (all: %v)", stem, sizes[stem], n, sizes)
		}
	}
	if len(sizes) != len(want) {
		t.Errorf("unexpected files: %v", sizes)
	}
	rel := got["relation"]
	if !sort.SliceIsSorted(rel, func(a, b int) bool { return rel[a].name < rel[b].name }) {
		t.Errorf("relation.go not sorted: %v", rel)
	}
}

func TestGroupFilesLettersDeepen(t *testing.T) {
	var funcs []groupFunc
	for _, s := range []string{"sort_a", "sort_b", "sort_c", "sort_d", "sort_e", "sortie", "so_x", "st_y", "sub_z"} {
		funcs = append(funcs, groupFunc{name: "F_" + s, symbol: s, size: 30})
	}
	got := groupFiles(funcs, map[string]bool{}, 100, 100, 1000)
	names := map[string]int{}
	for stem, fs := range got {
		names[stem] = len(fs)
	}
	// all singletons → misc (270 bytes > 100) → s (270) → so (210),
	// st_y and sub_z alone → sor (180) and so_x alone → sort (180) →
	// sorti alone, sort_ skipped → sort_a..e alone
	want := map[string]int{"misc_st_y": 1, "misc_sub_z": 1, "misc_so_x": 1, "misc_sortie": 1,
		"misc_sort_a": 1, "misc_sort_b": 1, "misc_sort_c": 1, "misc_sort_d": 1, "misc_sort_e": 1}
	if !reflect.DeepEqual(names, want) {
		t.Errorf("letters split: got %v, want %v", names, want)
	}
	total := 0
	for _, n := range names {
		total += n
	}
	if total != 9 {
		t.Errorf("lost functions: %v", names)
	}
}

func TestGroupFileName(t *testing.T) {
	reserved := map[string]bool{"p2": true, "alias": true}
	cases := map[string]string{
		"heap":            "heap.go",
		"pg_wasm":         "pg_wasm_.go",
		"port_linux":      "port_linux_.go",
		"foo_linux_amd64": "foo_linux_amd64__.go",
		"regress_test":    "regress_test_.go",
		"alias":           "alias_.go",
		"p2":              "p2_.go",
		"linux":           "linux.go",
		"_x":              "x.go",
		"Pg.Stat":         "pg_stat.go",
		"aux":             "aux_.go", // Windows device name
		"COM1":            "com1_.go",
		"lpt0":            "lpt0_.go", // refused by Git for Windows only
		"com0":            "com0.go",
		"auxiliary":       "auxiliary.go",
	}
	for stem, want := range cases {
		if got := groupFileName(stem, reserved); got != want {
			t.Errorf("%s: got %s, want %s", stem, got, want)
		}
	}
}
