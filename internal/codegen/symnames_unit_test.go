package codegen

import (
	"testing"

	"github.com/goccy/wasm2go/internal/wasm"
)

func TestSymbolFuncNamesDuplicatesAndGaps(t *testing.T) {
	mod := &wasm.Module{
		NumImportedFuncs: 1,
		Functions:        make([]wasm.Function, 5),
		FuncNames: map[uint32]string{
			0: "imported",
			1: "cleanup",
			2: "cleanup",
			3: "cleanup_1", // linker-suffixed duplicate: stripped, becomes the third namesake
			4: "9lives.cold",
			// 5: unnamed → keeps Fn5
		},
	}
	got := symbolFuncNames(mod, true)
	want := map[uint32]string{
		1: "F_cleanup_1",
		2: "F_cleanup_2",
		3: "F_cleanup_3",
		4: "F__9lives_x2ecold",
	}
	checkNames(t, got, want)
	if _, ok := got[5]; ok {
		t.Errorf("unnamed function 5 got a symbol name %q", got[5])
	}
	if lower := symbolFuncNames(mod, false); lower[4] != "f__9lives_x2ecold" {
		t.Errorf("single-package prefix: got %q", lower[4])
	}
}

// Binaryen suffixes repeated names with a pre-optimization function
// index. The suffix is dropped when the bare name exists or when the
// number is larger than the function's own index (the bare namesake
// was eliminated), and kept for a name that merely ends in a small
// number.
func TestSymbolFuncNamesLinkerSuffix(t *testing.T) {
	mod := &wasm.Module{
		NumImportedFuncs: 2,
		Functions:        make([]wasm.Function, 6),
		FuncNames: map[uint32]string{
			2: "heap_getattr",
			3: "heap_getattr_1883",
			4: "heap_getattr_2196",
			5: "pg_crc32c_2",   // 2 < index 5, no bare "pg_crc32c" → kept
			6: "utf8_to_utf16", // no digit suffix
			7: "cleanup_7293",  // 7293 > index 7, bare "cleanup" eliminated → stripped
		},
	}
	checkNames(t, symbolFuncNames(mod, true), map[uint32]string{
		2: "F_heap_getattr_1",
		3: "F_heap_getattr_2",
		4: "F_heap_getattr_3",
		5: "F_pg_crc32c_2",
		6: "F_utf8_to_utf16",
		7: "F_cleanup",
	})
}

func checkNames(t *testing.T, got, want map[uint32]string) {
	t.Helper()
	if len(got) != len(want) {
		t.Errorf("got %d names, want %d: %v", len(got), len(want), got)
	}
	for idx, name := range want {
		if got[idx] != name {
			t.Errorf("func %d: got %q, want %q", idx, got[idx], name)
		}
	}
}
