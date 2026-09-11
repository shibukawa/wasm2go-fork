package gcasm

import "testing"

// Go 1.27 prints data symbol headers as "... size=N align=0xM"; the
// jump tables of every function were silently dropped before the regex
// accepted the suffix.
func TestParseListingDataAlignSuffix(t *testing.T) {
	listing := "example.com/p.F STEXT size=16 align=0x0 args=0x8 locals=0x0 funcid=0x0\n" +
		"\t0x0000 00000 (f.go:1)\tTEXT\texample.com/p.F(SB), ABIInternal, $0-8\n" +
		"example.com/p.F.jump0 SRODATA static size=16 align=0x0\n" +
		"\t0x0000 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00  ................\n" +
		"\trel 0+8 t=R_ADDR example.com/p.F+10\n" +
		"\trel 8+8 t=R_ADDR example.com/p.F+20\n" +
		"example.com/p.F.arginfo1 SRODATA static dupok size=3 align=0x1\n"
	_, datas, err := ParseListing(listing)
	if err != nil {
		t.Fatal(err)
	}
	if len(datas) != 2 {
		t.Fatalf("want 2 data symbols, got %d", len(datas))
	}
	var jt *DataSym
	for _, d := range datas {
		if d.Name == "example.com/p.F.jump0" {
			jt = d
		}
	}
	if jt == nil || jt.Size != 16 || len(jt.Relocs) != 2 {
		t.Fatalf("jump table not parsed: %+v", jt)
	}
	if jt.Relocs[1].Off != 8 || jt.Relocs[1].Addend != 20 {
		t.Fatalf("relocs: %+v", jt.Relocs)
	}
}
