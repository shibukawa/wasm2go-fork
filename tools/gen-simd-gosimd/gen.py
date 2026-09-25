#!/usr/bin/env python3
"""Generates the simd/archsimd helper set for wasm2go's -gosimd mode.

Reads every SIMD helper signature from internal/codegen/helpers
(simd_scalar.go for the pure lane ops, helpers.go for the memory ops)
and emits, per architecture, a simd_g_<op> counterpart that carries the
v128 as an archsimd 128-bit vector (V128 = archsimd.Uint64x2) instead of
the [2]uint64 pair:

  simd_g_go127_amd64.go   //go:build goexperiment.simd && go1.27 && !go1.28 && amd64  (AVX/AVX2)
  simd_g_go127_arm64.go   //go:build goexperiment.simd && go1.27 && !go1.28 && arm64  (NEON)
  simd_g_go127_matrix_test.go  differential test: every simd_g_<op> must
                          agree with simd_<op>_scalar on the corpus.

The files are pinned to one Go release (VERSION below) because the
archsimd API is not covered by the compatibility promise and did change
between 1.26 and 1.27. Supporting a new release means adding a target
here (new VERSION/VERSION_TAG, templates adjusted to that API) and
teaching the emitter's -simd=<target> option about it.

An op with a native template (common_native / amd64_native / arm64_native) becomes a short inlinable
method chain; the compiler keeps the vector in a register across the
inlined call. Everything else is BRIDGED: the vector is spilled to a
[2]uint64, the existing simd_<op> helper (asm or scalar) runs, and the
result is loaded back. Bridging is always correct (it reuses the reference
implementation), just not fast, so the native table is what to grow.

simd/archsimd is not covered by the Go 1 compatibility promise and its API
moved between Go 1.26 and 1.27: this file and the two generated helper
files are the ONLY places that name it. Generated module code sees just
V128 and Simd_g_*.

Lane ops (extract/replace/load_lane/store_lane) get one specialization
per lane, simd_g_<op>_l<N>, because archsimd's GetElem/SetElem want a
constant index; the emitter folds the lane immediate into the name.

Usage: python3 tools/gen-simd-gosimd/gen.py  (from the repo root)
"""

import os
import re
import subprocess
import sys

# The Go release whose simd/archsimd API the templates target. The build
# tag pins the generated files to that release: a later Go (whose API may
# differ) simply compiles the pure fallback variant until a target for it
# exists here.
VERSION = "go127"
VERSION_TAG = "go1.27 && !go1.28"

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HELPERS = os.path.join(ROOT, "internal", "codegen", "helpers")

# ---------------------------------------------------------------------------
# Signature parsing

SIG_SCALAR = re.compile(r"^func (simd_\w+)_scalar\((.*?)\) (\S+) \{", re.M)
SIG_MEM = re.compile(r"^func (simd_(?:m64_)?v128_\w+|simd_f16x4_cvt)\((.*?)\) (\S+) \{", re.M)


def parse_params(s):
    """'a, b [2]uint64, lane int32' -> [('a','[2]uint64'),('b','[2]uint64'),('lane','int32')]"""
    out = []
    pending = []
    for part in [p.strip() for p in s.split(",") if p.strip()]:
        toks = part.split()
        if len(toks) == 1:
            pending.append(toks[0])
        else:
            name, typ = toks[0], " ".join(toks[1:])
            for p in pending:
                out.append((p, typ))
            pending = []
            out.append((name, typ))
    assert not pending, s
    return out


def read(path):
    with open(path) as f:
        return f.read()


def collect_ops():
    ops = []
    seen = set()
    for m in SIG_SCALAR.finditer(read(os.path.join(HELPERS, "simd_scalar.go"))):
        name, params, ret = m.group(1), parse_params(m.group(2)), m.group(3)
        if name in seen:
            continue
        seen.add(name)
        ops.append((name, params, ret, False))
    for m in SIG_MEM.finditer(read(os.path.join(HELPERS, "helpers.go"))):
        name, params, ret = m.group(1), parse_params(m.group(2)), m.group(3)
        if name in seen or name.startswith("simd_p_"):
            continue
        seen.add(name)
        ops.append((name, params, ret, True))
    return ops


def gotype(t):
    return "V128" if t == "[2]uint64" else t


def lanes_of(name):
    for pre, n in (("i8x16", 16), ("i16x8", 8), ("i32x4", 4), ("i64x2", 2), ("f32x4", 4), ("f64x2", 2)):
        if pre in name:
            return n
    for bits, n in (("load8_lane", 16), ("load16_lane", 8), ("load32_lane", 4), ("load64_lane", 2),
                    ("store8_lane", 16), ("store16_lane", 8), ("store32_lane", 4), ("store64_lane", 2)):
        if bits in name:
            return n
    raise ValueError(name)


# ---------------------------------------------------------------------------
# Native templates. Each is the BODY of the helper (statements ending in a
# return), written against the parameter names of the reference signature.
# {L} is the lane number in per-lane specializations.
#
# Lane views go through the Reshape/BitsTo/ToBits family, the one
# reinterpretation API both architectures share (amd64 also has As<T>,
# arm64 does not). All of them are free at the instruction level.

T = {"i8": "Int8x16", "u8": "Uint8x16", "i16": "Int16x8", "u16": "Uint16x8",
     "i32": "Int32x4", "u32": "Uint32x4", "i64": "Int64x2", "u64": "Uint64x2",
     "f32": "Float32x4", "f64": "Float64x2"}
MASKINT = {"i8": "Int8x16", "u8": "Int8x16", "i16": "Int16x8", "u16": "Int16x8",
           "i32": "Int32x4", "u32": "Int32x4", "i64": "Int64x2", "u64": "Int64x2",
           "f32": "Int32x4", "f64": "Int64x2"}
MASKT = {"i8": "i8", "u8": "i8", "i16": "i16", "u16": "i16", "i32": "i32", "u32": "i32",
         "i64": "i64", "u64": "i64", "f32": "i32", "f64": "i64"}
VIEW = {"u64": "", "u8": ".ReshapeToUint8s()", "u16": ".ReshapeToUint16s()", "u32": ".ReshapeToUint32s()",
        "i8": ".ReshapeToUint8s().BitsToInt8()", "i16": ".ReshapeToUint16s().BitsToInt16()",
        "i32": ".ReshapeToUint32s().BitsToInt32()", "i64": ".BitsToInt64()",
        "f32": ".ReshapeToUint32s().BitsToFloat32()", "f64": ".BitsToFloat64()"}
BACK = {"u64": "", "u8": ".ReshapeToUint64s()", "u16": ".ReshapeToUint64s()", "u32": ".ReshapeToUint64s()",
        "i8": ".ToBits().ReshapeToUint64s()", "i16": ".ToBits().ReshapeToUint64s()",
        "i32": ".ToBits().ReshapeToUint64s()", "i64": ".ToBits()",
        "f32": ".ToBits().ReshapeToUint64s()", "f64": ".ToBits()"}
LANES = {"i8": 16, "u8": 16, "i16": 8, "u16": 8, "i32": 4, "u32": 4, "i64": 2, "u64": 2, "f32": 4, "f64": 2}


def cast(t, x):
    """x (a V128) viewed as lane type t."""
    return x + VIEW[t]


def back(t):
    """suffix taking a lane-typed value back to the V128 carrier."""
    return BACK[t]


def conv(frm, to):
    """suffix converting a lane-typed value of type frm to type to."""
    if frm == to:
        return ""
    return BACK[frm] + VIEW[to]


LANE_BITS = {"i8": 8, "u8": 8, "i16": 16, "u16": 16, "i32": 32, "u32": 32, "i64": 64, "u64": 64, "f32": 32, "f64": 64}

# Lane-replicated constants the templates broadcast, collected as
# package-level [2]uint64 variables: gc does not hoist a Broadcast out of
# the loop that inlines it (three instructions per use, measured at 17%
# of a libwebp lossless encode inside narrow_u), whereas a package
# variable folds into the consuming instruction's memory operand.
KCONSTS = {}


def bc(t, val):
    """A vector with val in every lane. Literal values become package
    constants; anything else is a run-time Broadcast."""
    lit = None
    try:
        lit = int(str(val), 0)
    except ValueError:
        try:
            lit = float(val)
        except ValueError:
            lit = None
    if lit is None:
        return "archsimd.Broadcast%s(%s)" % (T[t], val)
    bits = LANE_BITS[t]
    if isinstance(lit, float):
        import struct
        if bits == 32:
            lane = struct.unpack("<I", struct.pack("<f", lit))[0]
        else:
            lane = struct.unpack("<Q", struct.pack("<d", lit))[0]
    else:
        lane = lit & ((1 << bits) - 1)
    word = 0
    for i in range(64 // bits):
        word |= lane << (bits * i)
    name = "simdGK_%s_%s" % (t, str(val).replace("-", "m").replace(".", "_"))
    KCONSTS[name] = (word, word)
    return kload(name, t)


def kload(name, t):
    return "archsimd.LoadUint64x2Array(&%s)%s" % (name, VIEW[t])


def binop(t, m, x="a", y="b"):
    return "return %s.%s(%s)%s" % (cast(t, x), m, cast(t, y), back(t))


def unop(t, m, x="a"):
    return "return %s.%s()%s" % (cast(t, x), m, back(t))


def cmp(t, m):
    return "return %s.%s(%s).To%s()%s" % (cast(t, "a"), m, cast(t, "b"), MASKINT[t], back(MASKT[t]))


def shift(t, m, bits):
    return "return %s.%s(uint64(s & %d))%s" % (cast(t, "a"), m, bits - 1, back(t))


def splat(t):
    conv_ = {"i8": "int8(x)", "i16": "int16(x)", "i32": "x", "i64": "x", "f32": "x", "f64": "x"}[t]
    return "return %s%s" % (bc(t, conv_), back(t))


def extract(t, signed=True):
    if t in ("i8", "i16"):
        if signed:
            return "return int32(%s.GetElem({L}))" % cast(t, "v")
        return "return int32(%s.GetElem({L}))" % cast("u" + t[1:], "v")
    return "return %s.GetElem({L})" % cast(t, "v")


def replace(t):
    conv_ = {"i8": "int8(x)", "i16": "int16(x)", "i32": "x", "i64": "x", "f32": "x", "f64": "x"}[t]
    return "return %s.SetElem({L}, %s)%s" % (cast(t, "v"), conv_, back(t))


def common_native():
    """Templates valid on both architectures (same archsimd method names)."""
    n = {}
    for t in ("i8", "i16", "i32", "i64", "f32", "f64"):
        n["simd_%sx%d_splat" % (t, LANES[t])] = splat(t)
    n["simd_v128_not"] = "return a.Not()"
    n["simd_v128_and"] = "return a.And(b)"
    n["simd_v128_andnot"] = "return a.AndNot(b)"
    n["simd_v128_or"] = "return a.Or(b)"
    n["simd_v128_xor"] = "return a.Xor(b)"
    n["simd_v128_bitselect"] = "return a.And(c).Or(b.AndNot(c))"
    # lane ops
    for t, pre in (("i8", "i8x16"), ("i16", "i16x8")):
        n["simd_%s_extract_lane_s" % pre] = extract(t, True)
        n["simd_%s_extract_lane_u" % pre] = extract(t, False)
        n["simd_%s_replace_lane" % pre] = replace(t)
    for t, pre in (("i32", "i32x4"), ("i64", "i64x2"), ("f32", "f32x4"), ("f64", "f64x2")):
        n["simd_%s_extract_lane" % pre] = extract(t)
        n["simd_%s_replace_lane" % pre] = replace(t)
    # integer compares
    for t, u, pre in (("i8", "u8", "i8x16"), ("i16", "u16", "i16x8"), ("i32", "u32", "i32x4")):
        n["simd_%s_eq" % pre] = cmp(t, "Equal")
        n["simd_%s_ne" % pre] = cmp(t, "NotEqual")
        n["simd_%s_lt_s" % pre] = cmp(t, "Less")
        n["simd_%s_gt_s" % pre] = cmp(t, "Greater")
        n["simd_%s_le_s" % pre] = cmp(t, "LessEqual")
        n["simd_%s_ge_s" % pre] = cmp(t, "GreaterEqual")
        n["simd_%s_lt_u" % pre] = cmp(u, "Less")
        n["simd_%s_gt_u" % pre] = cmp(u, "Greater")
        n["simd_%s_le_u" % pre] = cmp(u, "LessEqual")
        n["simd_%s_ge_u" % pre] = cmp(u, "GreaterEqual")
    for op, m in (("eq", "Equal"), ("ne", "NotEqual"), ("lt_s", "Less"), ("gt_s", "Greater"),
                  ("le_s", "LessEqual"), ("ge_s", "GreaterEqual")):
        n["simd_i64x2_" + op] = cmp("i64", m)
    for t, pre in (("f32", "f32x4"), ("f64", "f64x2")):
        for op, m in (("eq", "Equal"), ("ne", "NotEqual"), ("lt", "Less"), ("gt", "Greater"),
                      ("le", "LessEqual"), ("ge", "GreaterEqual")):
            n["simd_%s_%s" % (pre, op)] = cmp(t, m)
    # integer arithmetic
    for t, u, pre in (("i8", "u8", "i8x16"), ("i16", "u16", "i16x8"), ("i32", "u32", "i32x4"), ("i64", "u64", "i64x2")):
        n["simd_%s_add" % pre] = binop(t, "Add")
        n["simd_%s_sub" % pre] = binop(t, "Sub")
        n["simd_%s_neg" % pre] = unop(t, "Neg")
        if t != "i64":
            n["simd_%s_min_s" % pre] = binop(t, "Min")
            n["simd_%s_max_s" % pre] = binop(t, "Max")
            n["simd_%s_min_u" % pre] = binop(u, "Min")
            n["simd_%s_max_u" % pre] = binop(u, "Max")
            n["simd_%s_abs" % pre] = unop(t, "Abs")
    for t, u, pre in (("i8", "u8", "i8x16"), ("i16", "u16", "i16x8")):
        n["simd_%s_add_sat_s" % pre] = binop(t, "AddSaturated")
        n["simd_%s_sub_sat_s" % pre] = binop(t, "SubSaturated")
        n["simd_%s_add_sat_u" % pre] = binop(u, "AddSaturated")
        n["simd_%s_sub_sat_u" % pre] = binop(u, "SubSaturated")
        n["simd_%s_avgr_u" % pre] = binop(u, "Average")
    n["simd_i16x8_mul"] = binop("i16", "Mul")
    n["simd_i32x4_mul"] = binop("i32", "Mul")
    for t, pre, bits in (("i16", "i16x8", 16), ("i32", "i32x4", 32), ("i64", "i64x2", 64)):
        n["simd_%s_shl" % pre] = shift(t, "ShiftAllLeft", bits)
        n["simd_%s_shr_u" % pre] = shift("u" + t[1:], "ShiftAllRight", bits)
        if t != "i64":
            n["simd_%s_shr_s" % pre] = shift(t, "ShiftAllRight", bits)
    # floats
    for t, pre in (("f32", "f32x4"), ("f64", "f64x2")):
        for op, m in (("add", "Add"), ("sub", "Sub"), ("mul", "Mul"), ("div", "Div")):
            n["simd_%s_%s" % (pre, op)] = binop(t, m)
        for op, m in (("abs", "Abs"), ("neg", "Neg"), ("sqrt", "Sqrt"), ("ceil", "Ceil"),
                      ("floor", "Floor"), ("trunc", "Trunc"), ("nearest", "Round")):
            n["simd_%s_%s" % (pre, op)] = unop(t, m)
        # pmin(a,b) = b < a ? b : a ; pmax(a,b) = a < b ? b : a
        n["simd_%s_pmin" % pre] = "x, y := %s, %s\n\treturn y.IfElse(y.Less(x), x)%s" % (cast(t, "a"), cast(t, "b"), back(t))
        n["simd_%s_pmax" % pre] = "x, y := %s, %s\n\treturn y.IfElse(x.Less(y), x)%s" % (cast(t, "a"), cast(t, "b"), back(t))
    n["simd_f32x4_convert_i32x4_s"] = "return %s.ConvertToFloat32()%s" % (cast("i32", "a"), back("f32"))
    n["simd_f32x4_demote_f64x2_zero"] = "return %s.ConvertToFloat32()%s" % (cast("f64", "a"), back("f32"))
    # extend low: identical on both (VPMOVSX / SSHLL)
    n["simd_i16x8_extend_low_i8x16_s"] = "return %s.ExtendLo8ToInt16()%s" % (cast("i8", "a"), back("i16"))
    n["simd_i16x8_extend_low_i8x16_u"] = "return %s.ExtendLo8ToUint16()%s" % (cast("u8", "a"), back("u16"))
    n["simd_i32x4_extend_low_i16x8_s"] = "return %s.ExtendLo4ToInt32()%s" % (cast("i16", "a"), back("i32"))
    n["simd_i32x4_extend_low_i16x8_u"] = "return %s.ExtendLo4ToUint32()%s" % (cast("u16", "a"), back("u32"))
    n["simd_i64x2_extend_low_i32x4_s"] = "return %s.ExtendLo2ToInt64()%s" % (cast("i32", "a"), back("i64"))
    n["simd_i64x2_extend_low_i32x4_u"] = "return %s.ExtendLo2ToUint64()" % cast("u32", "a")
    # memory (32-bit and memory64 address forms share the templates; the
    # effective-address check is the existing simdEA / simdEA64)
    for pfx, ea in (("simd_v128_", "simd_g_ea(m, addr, offset, %d)"), ("simd_m64_v128_", "simd_g_ea64(m, addr, offset, %d)")):
        n[pfx + "load"] = "return archsimd.LoadUint64x2Array((*[2]uint64)(unsafe.Add(m.M, %s)))" % (ea % 16)
        n[pfx + "store"] = "v.StoreArray((*[2]uint64)(unsafe.Add(m.M, uintptr(%s))))\n\treturn 0" % (ea % 16)
        if pfx == "simd_v128_":
            n[pfx + "load_nc"] = "return archsimd.LoadUint64x2Array((*[2]uint64)(unsafe.Add(m.M, uintptr(uint64(uint32(addr))+uint64(uint32(offset))))))"
        else:
            n[pfx + "load_nc"] = "return archsimd.LoadUint64x2Array((*[2]uint64)(unsafe.Add(m.M, uintptr(uint64(addr)+uint64(offset)))))"
        for bits, gt, t in ((8, "uint8", "u8"), (16, "uint16", "u16"), (32, "uint32", "u32"), (64, "uint64", "u64")):
            n[pfx + "load%d_splat" % bits] = "return %s%s" % (bc(t, "*(*%s)(unsafe.Add(m.M, uintptr(%s)))" % (gt, ea % (bits // 8))), back(t))
            n[pfx + "load%d_lane" % bits] = "return %s.SetElem({L}, *(*%s)(unsafe.Add(m.M, uintptr(%s))))%s" % (cast(t, "v"), gt, ea % (bits // 8), back(t))
            n[pfx + "store%d_lane" % bits] = "*(*%s)(unsafe.Add(m.M, uintptr(%s))) = %s.GetElem({L})\n\treturn 0" % (gt, ea % (bits // 8), cast(t, "v"))
        n[pfx + "load32_zero"] = "var z archsimd.Uint32x4\n\treturn z.SetElem(0, *(*uint32)(unsafe.Add(m.M, uintptr(%s))))%s" % (ea % 4, back("u32"))
        n[pfx + "load64_zero"] = "var z archsimd.Uint64x2\n\treturn z.SetElem(0, *(*uint64)(unsafe.Add(m.M, uintptr(%s))))" % (ea % 8)
        # widening loads: the 8 source bytes ride the low half of a broadcast
        # (register-only), then the common ExtendLo op widens them.
        for src, st, m, dt in (("8x8_s", "i8", "ExtendLo8ToInt16", "i16"), ("8x8_u", "u8", "ExtendLo8ToUint16", "u16"),
                               ("16x4_s", "i16", "ExtendLo4ToInt32", "i32"), ("16x4_u", "u16", "ExtendLo4ToUint32", "u32"),
                               ("32x2_s", "i32", "ExtendLo2ToInt64", "i64"), ("32x2_u", "u32", "ExtendLo2ToUint64", "u64")):
            n[pfx + "load" + src] = "return %s.%s()%s" % (cast(st, bc("u64", "*(*uint64)(unsafe.Add(m.M, uintptr(%s)))" % (ea % 8))), m, back(dt))
    n["simd_v128_load_rng"] = ("start := int64(uint64(uint32(addr))) + int64(rlo)\n"
                               "\tif start < 0 || uint64(start)+uint64(uint32(span)) > m.memSize.Load() {\n\t\tpanic(simdGOOB)\n\t}\n"
                               "\treturn archsimd.LoadUint64x2Array((*[2]uint64)(unsafe.Add(m.M, uintptr(uint64(uint32(addr))+uint64(uint32(offset))))))")
    n["simd_m64_v128_load_rng"] = ("start := addr + rlo\n"
                                   "\tif start < 0 || uint64(start)+uint64(span) > m.memSize.Load() {\n\t\tpanic(simdGOOB)\n\t}\n"
                                   "\treturn archsimd.LoadUint64x2Array((*[2]uint64)(unsafe.Add(m.M, uintptr(uint64(addr)+uint64(offset)))))")
    return n


def amd64_native():
    n = common_native()
    # AVX has no 8-bit shifts: shift the 16-bit lanes and mask the bits that
    # crossed a byte boundary (LLVM's own lowering).
    n["simd_i8x16_shl"] = ("k := uint64(s & 7)\n"
                           "\treturn %s.ShiftAllLeft(k)%s.And(%s)%s" % (cast("u16", "a"), conv("u16", "u8"), bc("u8", "uint8(0xff << k)"), back("u8")))
    n["simd_i8x16_shr_u"] = ("k := uint64(s & 7)\n"
                             "\treturn %s.ShiftAllRight(k)%s.And(%s)%s" % (cast("u16", "a"), conv("u16", "u8"), bc("u8", "uint8(0xff >> k)"), back("u8")))
    n["simd_i8x16_shr_s"] = ("k := uint64(s & 7)\n"
                             "\tx := %s\n"
                             "\tlo := x.ShiftAllLeft(8).ShiftAllRight(k + 8).And(%s)\n"
                             "\thi := x.ShiftAllRight(k).And(%s)\n"
                             "\treturn hi.Or(lo)%s" % (cast("i16", "a"), bc("i16", "0x00ff"), bc("i16", "-256"), back("i16")))
    # 64-bit arithmetic shift is AVX-512 only: logical shift, then sign-fix
    # via ((u ^ t) - t) with t = 1<<(63-k).
    n["simd_i64x2_shr_s"] = ("k := uint64(s & 63)\n"
                             "\tt := archsimd.BroadcastUint64x2(0x8000000000000000 >> k)\n"
                             "\treturn a.ShiftAllRight(k).Xor(t).Sub(t)")
    n["simd_i64x2_abs"] = ("x := %s\n"
                           "\tvar z archsimd.Int64x2\n"
                           "\treturn x.IfElse(x.GreaterEqual(z), z.Sub(x))%s" % (cast("i64", "a"), back("i64")))
    # 64x64 multiply from 32x32->64 partial products (VPMULUDQ).
    n["simd_i64x2_mul"] = ("x, y := %s, %s\n"
                           "\tlo := x.MulWidenEven(y)\n"
                           "\tt1 := a.ShiftAllRight(32)%s.MulWidenEven(y)\n"
                           "\tt2 := x.MulWidenEven(b.ShiftAllRight(32)%s)\n"
                           "\treturn lo.Add(t1.Add(t2).ShiftAllLeft(32))" % (cast("u32", "a"), cast("u32", "b"), VIEW["u32"], VIEW["u32"]))
    n["simd_v128_any_true"] = "if a.IsZero() {\n\t\treturn 0\n\t}\n\treturn 1"
    for t, pre in (("i8", "i8x16"), ("i16", "i16x8"), ("i32", "i32x4"), ("i64", "i64x2")):
        n["simd_%s_all_true" % pre] = ("var z archsimd.%s\n\tif %s.Equal(z).ToBits() == 0 {\n\t\treturn 1\n\t}\n\treturn 0" % (T[t], cast(t, "a")))
        n["simd_%s_bitmask" % pre] = ("var z archsimd.%s\n\treturn int32(%s.Less(z).ToBits())" % (T[t], cast(t, "a")))
    # popcnt via nibble table lookup (VPSHUFB).
    n["simd_i8x16_popcnt"] = ("lut := %s\n"
                              "\tm := %s\n"
                              "\tlo := %s.And(m)\n"
                              "\thi := %s.ShiftAllRight(4)%s.And(m)\n"
                              "\treturn lut.PermuteOrZero(lo).Add(lut.PermuteOrZero(hi))%s"
                              % (kload("simdGPopcntLUT", "i8"), kload("simdGNibbleMask", "i8"), cast("i8", "a"), cast("u16", "a"), conv("u16", "i8"), back("i8")))
    # narrow 16->8: no VPACKSSWB in archsimd at AVX level; clamp then
    # gather the low bytes of each lane with two VPSHUFB.
    pack = ("\treturn x.PermuteOrZero(%s).Or(y.PermuteOrZero(%s))%s" % (kload("simdGPackLo", "i8"), kload("simdGPackHi", "i8"), back("i8")))
    n["simd_i8x16_narrow_i16x8_s"] = ("x := %s.Max(%s).Min(%s)%s\n"
                                      "\ty := %s.Max(%s).Min(%s)%s\n%s"
                                      % (cast("i16", "a"), bc("i16", "-128"), bc("i16", "127"), conv("i16", "i8"),
                                         cast("i16", "b"), bc("i16", "-128"), bc("i16", "127"), conv("i16", "i8"), pack))
    n["simd_i8x16_narrow_i16x8_u"] = ("var z archsimd.Int16x8\n"
                                      "\tx := %s.Max(z).Min(%s)%s\n"
                                      "\ty := %s.Max(z).Min(%s)%s\n%s"
                                      % (cast("i16", "a"), bc("i16", "255"), conv("i16", "i8"),
                                         cast("i16", "b"), bc("i16", "255"), conv("i16", "i8"), pack))
    n["simd_i16x8_narrow_i32x4_s"] = "return %s.SaturateToInt16Concat(%s)%s" % (cast("i32", "a"), cast("i32", "b"), back("i16"))
    n["simd_i16x8_narrow_i32x4_u"] = "return %s.SaturateToUint16Concat(%s)%s" % (cast("i32", "a"), cast("i32", "b"), back("u16"))
    hi = "%s.PermuteScalars(2, 3, 2, 3)" % cast("i32", "a")
    n["simd_i16x8_extend_high_i8x16_s"] = "return %s%s.ExtendLo8ToInt16()%s" % (hi, conv("i32", "i8"), back("i16"))
    n["simd_i16x8_extend_high_i8x16_u"] = "return %s%s.ExtendLo8ToUint16()%s" % (hi, conv("i32", "u8"), back("u16"))
    n["simd_i32x4_extend_high_i16x8_s"] = "return %s%s.ExtendLo4ToInt32()%s" % (hi, conv("i32", "i16"), back("i32"))
    n["simd_i32x4_extend_high_i16x8_u"] = "return %s%s.ExtendLo4ToUint32()%s" % (hi, conv("i32", "u16"), back("u32"))
    n["simd_i64x2_extend_high_i32x4_s"] = "return %s.ExtendLo2ToInt64()%s" % (hi, back("i64"))
    n["simd_i64x2_extend_high_i32x4_u"] = "return %s%s.ExtendLo2ToUint64()" % (hi, conv("i32", "u32"))
    # extmul: widen both halves, multiply at the wide width.
    for half in ("low", "high"):
        def sel(x, t):
            if half == "low":
                return cast(t, x)
            return "%s.PermuteScalars(2, 3, 2, 3)%s" % (cast("i32", x), conv("i32", t))
        n["simd_i16x8_extmul_%s_i8x16_s" % half] = "return %s.ExtendLo8ToInt16().Mul(%s.ExtendLo8ToInt16())%s" % (sel("a", "i8"), sel("b", "i8"), back("i16"))
        n["simd_i16x8_extmul_%s_i8x16_u" % half] = "return %s.ExtendLo8ToUint16().Mul(%s.ExtendLo8ToUint16())%s" % (sel("a", "u8"), sel("b", "u8"), back("u16"))
        n["simd_i32x4_extmul_%s_i16x8_s" % half] = "return %s.ExtendLo4ToInt32().Mul(%s.ExtendLo4ToInt32())%s" % (sel("a", "i16"), sel("b", "i16"), back("i32"))
        n["simd_i32x4_extmul_%s_i16x8_u" % half] = "return %s.ExtendLo4ToUint32().Mul(%s.ExtendLo4ToUint32())%s" % (sel("a", "u16"), sel("b", "u16"), back("u32"))
    # 32->64 extmul: VPMULDQ takes the even lanes, so spread lanes (0,1) or
    # (2,3) over the even positions first.
    for half, p in (("low", "0, 0, 1, 1"), ("high", "2, 2, 3, 3")):
        n["simd_i64x2_extmul_%s_i32x4_s" % half] = "return %s.PermuteScalars(%s).MulWidenEven(%s.PermuteScalars(%s))%s" % (cast("i32", "a"), p, cast("i32", "b"), p, back("i64"))
        n["simd_i64x2_extmul_%s_i32x4_u" % half] = "return %s.PermuteScalars(%s)%s.MulWidenEven(%s.PermuteScalars(%s)%s)" % (cast("i32", "a"), p, conv("i32", "u32"), cast("i32", "b"), p, conv("i32", "u32"))
    # pairwise widening adds ride VPMADDUBSW / VPMADDWD against a vector of ones.
    n["simd_i16x8_extadd_pairwise_i8x16_s"] = "return %s.DotProductPairsSaturated(%s)%s" % (bc("u8", "1"), cast("i8", "a"), back("i16"))
    n["simd_i16x8_extadd_pairwise_i8x16_u"] = "return %s.DotProductPairsSaturated(%s)%s" % (cast("u8", "a"), bc("i8", "1"), back("i16"))
    n["simd_i32x4_extadd_pairwise_i16x8_s"] = "return %s.DotProductPairs(%s)%s" % (cast("i16", "a"), bc("i16", "1"), back("i32"))
    n["simd_i32x4_extadd_pairwise_i16x8_u"] = ("x := %s.Xor(%s)\n"
                                               "\treturn x.DotProductPairs(%s).Add(%s)%s" % (cast("i16", "a"), bc("i16", "-0x8000"), bc("i16", "1"), bc("i32", "0x10000"), back("i32")))
    n["simd_i32x4_dot_i16x8_s"] = "return %s.DotProductPairs(%s)%s" % (cast("i16", "a"), cast("i16", "b"), back("i32"))
    # q15mulr: full 32-bit products from the low/high 16-bit halves, round,
    # shift, and pack with saturation.
    n["simd_i16x8_q15mulr_sat_s"] = ("x, y := %s, %s\n"
                                     "\tlo, hi := x.Mul(y), x.MulHigh(y)\n"
                                     "\tr := %s\n"
                                     "\tp0 := lo.InterleaveLo(hi)%s.Add(r).ShiftAllRight(15)\n"
                                     "\tp1 := lo.InterleaveHi(hi)%s.Add(r).ShiftAllRight(15)\n"
                                     "\treturn p0.SaturateToInt16Concat(p1)%s"
                                     % (cast("i16", "a"), cast("i16", "b"), bc("i32", "0x4000"), conv("i16", "i32"), conv("i16", "i32"), back("i16")))
    # IEEE min/max with wasm NaN and signed-zero rules. VMINPS/VMAXPS
    # return the second operand on NaN, but gc treats Min/Max as
    # commutative and may swap or CSE the operands, so the sequence is
    # built from compares only: a<b ? a : b, equal lanes get the bitwise
    # or/and (so -0 orders before +0), NaN lanes a canonical NaN.
    for t, pre, ut, qnan in (("f32", "f32x4", "u32", "0x7fc00000"), ("f64", "f64x2", "u64", "0x7ff8000000000000")):
        for op, pick, tie in (("min", "lt", "Or"), ("max", "gt", "And")):
            n["simd_%s_%s" % (pre, op)] = ("x, y := %s, %s\n"
                                          "\tsel := %s.To%s()%s\n"
                                          "\teq := x.Equal(y).To%s()%s\n"
                                          "\tnan := x.IsNaN().Or(y.IsNaN()).To%s()%s\n"
                                          "\tr := a.And(sel).Or(b.AndNot(sel))\n"
                                          "\tr = r.AndNot(eq).Or(a.%s(b).And(eq))\n"
                                          "\treturn r.AndNot(nan).Or(%s%s.And(nan))"
                                          % (cast(t, "a"), cast(t, "b"),
                                             "x.Less(y)" if pick == "lt" else "x.Greater(y)", MASKINT[t], back(MASKT[t]),
                                             MASKINT[t], back(MASKT[t]), MASKINT[t], back(MASKT[t]), tie, bc(ut, qnan), back(ut)))
    # trunc_sat: VCVTTPS2DQ yields INT_MIN for NaN and both overflows; fix
    # the NaN (-> 0) and positive-overflow (-> INT_MAX) lanes.
    n["simd_i32x4_trunc_sat_f32x4_s"] = ("x := %s\n"
                                         "\tr := x.ConvertToInt32()%s.And(x.Equal(x).ToInt32x4()%s)\n"
                                         "\tbig := x.GreaterEqual(%s).ToInt32x4()%s\n"
                                         "\treturn r.AndNot(big).Or(big.And(%s%s))"
                                         % (cast("f32", "a"), back("i32"), back("i32"), bc("f32", "2147483648.0"), back("i32"), bc("u32", "0x7fffffff"), back("u32")))
    n["simd_i32x4_trunc_sat_f64x2_s_zero"] = ("x := %s\n"
                                              "\tok := x.Equal(x).ToInt64x2()%s.PermuteScalars(0, 2, 2, 2)%s\n"
                                              "\tbig := x.GreaterEqual(%s).ToInt64x2()%s.PermuteScalars(0, 2, 2, 2)%s\n"
                                              "\tlowHalf := archsimd.LoadUint64x2Array(&simdGLowHalf)\n"
                                              "\tr := x.ConvertToInt32()%s.And(ok).And(lowHalf)\n"
                                              "\treturn r.AndNot(big).Or(big.And(%s%s).And(lowHalf))"
                                              % (cast("f64", "a"), conv("i64", "i32"), back("i32"), bc("f64", "2147483648.0"), conv("i64", "i32"), back("i32"),
                                                 back("i32"), bc("u32", "0x7fffffff"), back("u32")))
    # shuffles: VPSHUFB zeroes on bit 7; +112 saturating pushes every
    # out-of-range index there.
    n["simd_i8x16_swizzle"] = "return %s.PermuteOrZero(%s.AddSaturated(%s)%s)%s" % (cast("u8", "a"), cast("u8", "s"), bc("u8", "112"), conv("u8", "i8"), back("u8"))
    n["simd_i8x16_shuffle"] = ("p := %s\n"
                               "\tia := p.AddSaturated(%s)%s\n"
                               "\tib := p.Sub(%s).AddSaturated(%s)%s\n"
                               "\treturn %s.PermuteOrZero(ia).Or(%s.PermuteOrZero(ib))%s"
                               % (cast("u8", "pat"), bc("u8", "112"), conv("u8", "i8"), bc("u8", "16"), bc("u8", "112"), conv("u8", "i8"), cast("u8", "a"), cast("u8", "b"), back("u8")))
    return n


def arm64_native():
    n = common_native()
    n["simd_i8x16_shl"] = shift("i8", "ShiftAllLeft", 8)
    n["simd_i8x16_shr_s"] = shift("i8", "ShiftAllRight", 8)
    n["simd_i8x16_shr_u"] = shift("u8", "ShiftAllRight", 8)
    n["simd_i64x2_shr_s"] = shift("i64", "ShiftAllRight", 64)
    n["simd_i64x2_abs"] = unop("i64", "Abs")
    n["simd_i8x16_popcnt"] = unop("i8", "OnesCount")
    # 64x64 multiply from UMULL partial products; UZP1/UZP2 gather the
    # low/high 32-bit halves into the low lanes UMULL reads.
    n["simd_i64x2_mul"] = ("x, y := %s, %s\n"
                           "\txl, xh := x.ConcatEven(x), x.ConcatOdd(x)\n"
                           "\tyl, yh := y.ConcatEven(y), y.ConcatOdd(y)\n"
                           "\treturn xl.MulWidenLo(yl).Add(xh.MulWidenLo(yl).Add(xl.MulWidenLo(yh)).ShiftAllLeft(32))" % (cast("u32", "a"), cast("u32", "b")))
    n["simd_v128_any_true"] = "if a.GetElem(0)|a.GetElem(1) != 0 {\n\t\treturn 1\n\t}\n\treturn 0"
    for t, pre in (("i8", "i8x16"), ("i16", "i16x8"), ("i32", "i32x4"), ("i64", "i64x2")):
        n["simd_%s_all_true" % pre] = ("var z archsimd.%s\n\tm := %s.Equal(z).To%s()%s\n\tif m.GetElem(0)|m.GetElem(1) == 0 {\n\t\treturn 1\n\t}\n\treturn 0"
                                       % (T[t], cast(t, "a"), MASKINT[t], back(t)))
    # bitmask: sign lanes AND a lane-index bit vector, then a horizontal sum.
    n["simd_i8x16_bitmask"] = ("var z archsimd.Int8x16\n"
                               "\tm := %s.Less(z).ToInt8x16()%s.And(archsimd.LoadUint64x2Array(&simdGBits8))\n"
                               "\tlo := (m.GetElem(0) * 0x0101010101010101) >> 56\n"
                               "\thi := (m.GetElem(1) * 0x0101010101010101) >> 56\n"
                               "\treturn int32(lo | hi<<8)" % (cast("i8", "a"), back("i8")))
    n["simd_i16x8_bitmask"] = ("var z archsimd.Int16x8\n"
                               "\treturn int32(%s.Less(z).ToInt16x8()%s.And(%s).ReduceSum())" % (cast("i16", "a"), conv("i16", "u16"), kload("simdGBits16", "u16")))
    n["simd_i32x4_bitmask"] = ("var z archsimd.Int32x4\n"
                               "\treturn int32(%s.Less(z).ToInt32x4()%s.And(%s).ReduceSum())" % (cast("i32", "a"), conv("i32", "u32"), kload("simdGBits32", "u32")))
    n["simd_i64x2_bitmask"] = ("var z archsimd.Int64x2\n"
                               "\tm := %s.Less(z).ToInt64x2()%s.And(archsimd.LoadUint64x2Array(&simdGBits64))\n"
                               "\treturn int32(m.GetElem(0) | m.GetElem(1))" % (cast("i64", "a"), back("i64")))
    # narrow: SQXTN/SQXTUN pack into the low half; ZIP1.2D joins two halves.
    for op, st, m, dt in (("simd_i8x16_narrow_i16x8_s", "i16", "SaturateToInt8", "i8"), ("simd_i8x16_narrow_i16x8_u", "i16", "SaturateToUint8", "u8"),
                          ("simd_i16x8_narrow_i32x4_s", "i32", "SaturateToInt16", "i16"), ("simd_i16x8_narrow_i32x4_u", "i32", "SaturateToUint16", "u16")):
        n[op] = "return %s.%s()%s.InterleaveLo(%s.%s()%s)" % (cast(st, "a"), m, back(dt), cast(st, "b"), m, back(dt))
    n["simd_i16x8_extend_high_i8x16_s"] = "return %s.HiToLo().ExtendLo8ToInt16()%s" % (cast("i8", "a"), back("i16"))
    n["simd_i16x8_extend_high_i8x16_u"] = "return %s.HiToLo().ExtendLo8ToUint16()%s" % (cast("u8", "a"), back("u16"))
    n["simd_i32x4_extend_high_i16x8_s"] = "return %s.HiToLo().ExtendLo4ToInt32()%s" % (cast("i16", "a"), back("i32"))
    n["simd_i32x4_extend_high_i16x8_u"] = "return %s.HiToLo().ExtendLo4ToUint32()%s" % (cast("u16", "a"), back("u32"))
    n["simd_i64x2_extend_high_i32x4_s"] = "return %s.HiToLo().ExtendLo2ToInt64()%s" % (cast("i32", "a"), back("i64"))
    n["simd_i64x2_extend_high_i32x4_u"] = "return %s.HiToLo().ExtendLo2ToUint64()" % cast("u32", "a")
    for half, sel in (("low", "%s"), ("high", "%s.HiToLo()")):
        for wide, st, ut, dt, du in (("i16x8_extmul_%s_i8x16", "i8", "u8", "i16", "u16"), ("i32x4_extmul_%s_i16x8", "i16", "u16", "i32", "u32"),
                                     ("i64x2_extmul_%s_i32x4", "i32", "u32", "i64", "u64")):
            n["simd_" + wide % half + "_s"] = "return %s.MulWidenLo(%s)%s" % (sel % cast(st, "a"), sel % cast(st, "b"), back(dt))
            n["simd_" + wide % half + "_u"] = "return %s.MulWidenLo(%s)%s" % (sel % cast(ut, "a"), sel % cast(ut, "b"), back(du))
    n["simd_i16x8_extadd_pairwise_i8x16_s"] = "x := %s\n\treturn x.ExtendLo8ToInt16().ConcatAddPairs(x.HiToLo().ExtendLo8ToInt16())%s" % (cast("i8", "a"), back("i16"))
    n["simd_i16x8_extadd_pairwise_i8x16_u"] = "x := %s\n\treturn x.ExtendLo8ToUint16().ConcatAddPairs(x.HiToLo().ExtendLo8ToUint16())%s" % (cast("u8", "a"), back("u16"))
    n["simd_i32x4_extadd_pairwise_i16x8_s"] = "x := %s\n\treturn x.ExtendLo4ToInt32().ConcatAddPairs(x.HiToLo().ExtendLo4ToInt32())%s" % (cast("i16", "a"), back("i32"))
    n["simd_i32x4_extadd_pairwise_i16x8_u"] = "x := %s\n\treturn x.ExtendLo4ToUint32().ConcatAddPairs(x.HiToLo().ExtendLo4ToUint32())%s" % (cast("u16", "a"), back("u32"))
    n["simd_i32x4_dot_i16x8_s"] = ("x, y := %s, %s\n"
                                   "\treturn x.MulWidenLo(y).ConcatAddPairs(x.HiToLo().MulWidenLo(y.HiToLo()))%s" % (cast("i16", "a"), cast("i16", "b"), back("i32")))
    n["simd_i16x8_q15mulr_sat_s"] = ("x, y := %s, %s\n"
                                     "\tr := %s\n"
                                     "\tp0 := x.MulWidenLo(y).Add(r).ShiftAllRight(15).SaturateToInt16()%s\n"
                                     "\tp1 := x.HiToLo().MulWidenLo(y.HiToLo()).Add(r).ShiftAllRight(15).SaturateToInt16()%s\n"
                                     "\treturn p0.InterleaveLo(p1)" % (cast("i16", "a"), cast("i16", "b"), bc("i32", "0x4000"), back("i16"), back("i16")))
    # FMIN/FMAX propagate NaN and order -0 < +0: wasm semantics as is.
    for t, pre in (("f32", "f32x4"), ("f64", "f64x2")):
        n["simd_%s_min" % pre] = binop(t, "Min")
        n["simd_%s_max" % pre] = binop(t, "Max")
    n["simd_i32x4_trunc_sat_f32x4_s"] = "return %s.ConvertToInt32()%s" % (cast("f32", "a"), back("i32"))
    n["simd_i32x4_trunc_sat_f32x4_u"] = "return %s.ConvertToUint32()%s" % (cast("f32", "a"), back("u32"))
    n["simd_f32x4_convert_i32x4_u"] = "return %s.ConvertToFloat32()%s" % (cast("u32", "a"), back("f32"))
    n["simd_i32x4_trunc_sat_f64x2_s_zero"] = "return %s.ConvertToInt64().SaturateToInt32()%s" % (cast("f64", "a"), back("i32"))
    n["simd_i32x4_trunc_sat_f64x2_u_zero"] = "return %s.ConvertToUint64().SaturateToUint32()%s" % (cast("f64", "a"), back("u32"))
    n["simd_f64x2_convert_low_i32x4_s"] = "return %s.ExtendLo2ToInt64().ConvertToFloat64()%s" % (cast("i32", "a"), back("f64"))
    n["simd_f64x2_convert_low_i32x4_u"] = "return %s.ExtendLo2ToUint64().ConvertToFloat64()%s" % (cast("u32", "a"), back("f64"))
    n["simd_f64x2_promote_low_f32x4"] = "return %s.ConvertLo2ToFloat64()%s" % (cast("f32", "a"), back("f64"))
    # TBL zeroes every out-of-range index on its own.
    n["simd_i8x16_swizzle"] = "return %s.LookupOrZero(%s)%s" % (cast("u8", "a"), cast("u8", "s"), back("u8"))
    n["simd_i8x16_shuffle"] = ("p := %s\n"
                               "\treturn %s.LookupOrZero(p).Or(%s.LookupOrZero(p.Sub(%s)))%s"
                               % (cast("u8", "pat"), cast("u8", "a"), cast("u8", "b"), bc("u8", "16"), back("u8")))
    return n


# Pre-normalized shuffle forms the emitter uses for constant patterns: an
# index in 0..15 selects, 0x80 yields zero, on both architectures.
SHUFFLE_CONST = {
    "amd64": (
        "return %s.PermuteOrZero(%s).Or(%s.PermuteOrZero(%s))%s" % (cast("u8", "a"), cast("i8", "ia"), cast("u8", "b"), cast("i8", "ib"), back("u8")),
        "return %s.PermuteOrZero(%s)%s" % (cast("u8", "a"), cast("i8", "idx"), back("u8")),
    ),
    "arm64": (
        "return %s.LookupOrZero(%s).Or(%s.LookupOrZero(%s))%s" % (cast("u8", "a"), cast("u8", "ia"), cast("u8", "b"), cast("u8", "ib"), back("u8")),
        "return %s.LookupOrZero(%s)%s" % (cast("u8", "a"), cast("u8", "idx"), back("u8")),
    ),
}

CONSTS = {
    "simdGPopcntLUT": (0x0302020102010100, 0x0403030203020201),
    "simdGNibbleMask": (0x0f0f0f0f0f0f0f0f, 0x0f0f0f0f0f0f0f0f),
    "simdGPackLo": (0x0e0c0a0806040200, 0x8080808080808080),
    "simdGPackHi": (0x8080808080808080, 0x0e0c0a0806040200),
    "simdGLowHalf": (0xffffffffffffffff, 0),
    "simdGBits8": (0x8040201008040201, 0x8040201008040201),
    "simdGBits16": (0x0008000400020001, 0x0080004000200010),
    "simdGBits32": (0x0000000200000001, 0x0000000800000004),
    "simdGBits64": (1, 2),
}

HEADER = """// Code generated by tools/gen-simd-gosimd/gen.py; DO NOT EDIT.

//go:build goexperiment.simd && %s && %s

package helpers

// The -simd=%s helper set for %s: every simd_<op> lane/memory helper has a
// simd_g_<op> twin here whose v128 operands are archsimd 128-bit vectors,
// so a generated function body keeps its vectors in SIMD registers from
// one op to the next instead of round-tripping [2]uint64 pairs. Ops the
// generator maps natively are short inlinable method chains; the rest
// bridge to the existing helper through memory (correct, not fast — see
// tools/gen-simd-gosimd/gen.py to promote one).
//
// This file is the only generated-code surface that names simd/archsimd,
// which carries no compatibility promise: a Go release that changes the
// API is absorbed by regenerating this file, never the module code.

import (
\t"simd/archsimd"
\t"unsafe"
)

// V128 is the vector carrier of the -gosimd backend: a wasm v128 in an
// archsimd register type. Lane views are reinterpretations (As<T>),
// free at the instruction level.
type V128 = archsimd.Uint64x2

// simd_g_const loads a v128 literal the emitter parked in a package-level
// [2]uint64 (a memory operand the compiler folds into the consuming op).
func simd_g_const(p *[2]uint64) V128 { return archsimd.LoadUint64x2Array(p) }

// simd_g_from / simd_g_to bridge between the pair carrier and V128 at
// the boundaries where the pure helpers are reused.
func simd_g_from(p [2]uint64) V128 { return archsimd.LoadUint64x2Array(&p) }

func simd_g_to(v V128) [2]uint64 {
\tvar p [2]uint64
\tv.StoreArray(&p)
\treturn p
}

// simd_g_ea / simd_g_ea64 are the bounds checks of the memory helpers
// (the shape of simdEA / simdEA64), with the trap raised inline: a
// call to the shared trap function costs the inliner more than the
// whole helper is worth, and the loads would stop inlining.
const simdGOOB = "wasm: v128 memory access out of bounds"

func simd_g_ea(m *Module, addr int32, offset int32, size uint64) uintptr {
	ea := uint64(uint32(addr)) + uint64(uint32(offset))
	if ea+size > m.memSize.Load() {
		panic(simdGOOB)
	}
	return uintptr(ea)
}

func simd_g_ea64(m *Module, addr int64, offset int64, size uint64) uintptr {
	ea := uint64(addr) + uint64(offset)
	end := ea + size
	if ea < uint64(addr) || end < ea || end > m.memSize.Load() {
		panic(simdGOOB)
	}
	return uintptr(ea)
}

// simd_g_i8x16_shuffle2 is i8x16.shuffle with the emitter-normalized
// index vectors (0..15 selects from the respective source, 0x80 zeroes),
// and simd_g_i8x16_swizzle_c the single-source form for patterns that
// touch one operand only.
func simd_g_i8x16_shuffle2(a, b, ia, ib V128) V128 {
\t%s
}

func simd_g_i8x16_swizzle_c(a, idx V128) V128 {
\t%s
}

"""

INIT_AMD64 = """// The AVX-encoded 128-bit ops (and the AVX2 broadcasts) this file
// inlines need the CPU features at run time; a GOEXPERIMENT=simd build
// of a module therefore targets AVX2 hardware, which every x86-64
// since 2013 has. Fail fast rather than SIGILL mid-function.
func init() {
\tif !archsimd.X86.AVX2() {
\t\tpanic("wasm2go: this binary was built with GOEXPERIMENT=simd and needs an AVX2 CPU; rebuild without GOEXPERIMENT=simd for older hardware")
\t}
}

// simd_g_supported reports whether the archsimd backend can run here.
func simd_g_supported() bool { return archsimd.X86.AVX2() }

"""

INIT_ARM64 = """// NEON is baseline on every arm64 Go target.
func simd_g_supported() bool { return true }

"""


def render_sig(name, params, ret):
    ps = ", ".join("%s %s" % (p, gotype(t)) for p, t in params)
    return "func %s(%s) %s" % (name, ps, gotype(ret))


def bridge_body(name, params, ret):
    """Spill V128 params, call the existing helper, reload the result."""
    lines = []
    args = []
    for p, t in params:
        if t == "[2]uint64":
            lines.append("\tp_%s := simd_g_to(%s)" % (p, p))
            args.append("p_" + p)
        else:
            args.append(p)
    call = "%s(%s)" % (name, ", ".join(args))
    if ret == "[2]uint64":
        lines.append("\treturn simd_g_from(%s)" % call)
    else:
        lines.append("\treturn " + call)
    return "\n".join(lines)


def gen_arch(arch, ops):
    KCONSTS.clear()
    native = amd64_native() if arch == "amd64" else arm64_native()
    used = set()
    out = [HEADER % (VERSION_TAG, arch, VERSION, arch, SHUFFLE_CONST[arch][0], SHUFFLE_CONST[arch][1])]
    out.append(INIT_AMD64 if arch == "amd64" else INIT_ARM64)
    for cname, (lo, hi) in list(CONSTS.items()) + sorted(KCONSTS.items()):
        out.append("var %s = [2]uint64{0x%016x, 0x%016x}\n\n" % (cname, lo, hi))
    stats = {"native": 0, "bridge": 0}
    for name, params, ret, _mem in ops:
        g = "simd_g_" + name[len("simd_"):]
        lane = [p for p, t in params if p == "lane"]
        body = native.get(name)
        if body is not None:
            used.add(name)
            stats["native"] += 1
        else:
            stats["bridge"] += 1
        if lane:
            # Per-lane specializations; the lane parameter is dropped.
            nl = lanes_of(name)
            rest = [(p, t) for p, t in params if p != "lane"]
            for L in range(nl):
                if body is not None:
                    b = "\t" + body.replace("{L}", str(L))
                else:
                    b = bridge_body(name, [(p, t) if p != "lane" else ("%d" % L, "int32") for p, t in params], ret)
                    # the lane argument is a literal now
                    b = b.replace("p_%d" % L, "%d" % L)
                out.append("%s {\n%s\n}\n\n" % (render_sig("%s_l%d" % (g, L), rest, ret), b))
            continue
        b = ("\t" + body) if body is not None else bridge_body(name, params, ret)
        out.append("%s {\n%s\n}\n\n" % (render_sig(g, params, ret), b))
    unused = sorted(set(native) - used)
    if unused:
        sys.exit("%s: native templates without a helper signature: %s" % (arch, unused))
    src = "".join(out)
    if "unsafe." not in src:
        src = src.replace('\t"unsafe"\n', "")
    return src, stats


# ---------------------------------------------------------------------------
# Differential test

TEST_HEADER = """// Code generated by tools/gen-simd-gosimd/gen.py; DO NOT EDIT.

//go:build goexperiment.simd && @VTAG@ && (amd64 || arm64)

package helpers

// Every simd_g_<op> must agree with the pair-carrier helper simd_<op>
// (asm-backed on amd64.v2/arm64, the scalar reference elsewhere) on the
// corpus below (edge lanes plus seeded random vectors); memory
// helpers are checked against their pair-carrier originals over a small
// module, including the out-of-bounds trap.

import (
\t"math/rand"
\t"testing"
)

var simdGCorpus = func() [][2]uint64 {
\tvs := [][2]uint64{
\t\t{0, 0},
\t\t{^uint64(0), ^uint64(0)},
\t\t{0x8080808080808080, 0x8080808080808080},
\t\t{0x8000800080008000, 0x8000800080008000},
\t\t{0x8000000080000000, 0x8000000080000000},
\t\t{0x8000000000000000, 0x8000000000000000},
\t\t{0x7f7f7f7f7f7f7f7f, 0x0101010101010101},
\t\t{0x7fff7fff7fff7fff, 0xffffffffffffffff},
\t\t{0x7fffffff7fffffff, 0x0000000100000001},
\t\t{0x7fc000007fc00000, 0xffc00000ff800001}, // f32 NaNs / -inf+payload
\t\t{0x7ff8000000000000, 0xfff0000000000001}, // f64 NaN / sNaN
\t\t{0x3f8000004048f5c3, 0xc248f5c300000000}, // f32 1.0, pi-ish, -50.24, +0
\t\t{0x4f80000041dfffff, 0xcf000000c1dfffff}, // f32 2^32, near-2^31 bounds
\t\t{0x8000000000000000, 0x0000000000000000}, // f64 -0, +0
\t\t{0x41dfffffffc00000, 0x43f0000000000000}, // f64 2^31-1, 2^64
\t\t{0x0102030405060708, 0x090a0b0c0d0e0f10},
\t\t{0x1011121380402010, 0xfefdfcfb00ff7f80}, // swizzle/shuffle indices
\t\t{0x0000000000000000, 0x8000000000000000}, // f64 +0, -0 (order swap)
\t\t{0x3ff0000000000000, 0xc1e0000000000000}, // f64 1.0, -2^31
\t\t{0x41e0000000000000, 0x7ff0000000000000}, // f64 2^31, +inf
\t}
\tr := rand.New(rand.NewSource(1))
\tfor i := 0; i < 48; i++ {
\t\tvs = append(vs, [2]uint64{r.Uint64(), r.Uint64()})
\t}
\treturn vs
}()

var simdGShifts = []int32{0, 1, 7, 8, 15, 16, 31, 32, 63, 64, 65, -1}

var simdGScalars32 = []int32{0, 1, -1, 127, -128, 255, 0x7fff, -0x8000, 0x12345678, -0x7fffffff - 1}
var simdGScalars64 = []int64{0, 1, -1, 0x123456789abcdef0, -0x7fffffffffffffff - 1}
var simdGFloats32 = []float32{0, -0.0, 1.5, -2.25, 3.4e38, float32(nan32)}
var simdGFloats64 = []float64{0, -0.0, 1.5, -2.25, 1.7e308, nan64}

// Which NaN an operation propagates when several operands are NaN is
// nondeterministic in wasm (both sides return some arithmetic NaN); those
// lanes compare loosely, everything else bit-for-bit.
func simdGNaNLanes(name string) int {
\tswitch name {
\tcase "simd_f32x4_add", "simd_f32x4_sub", "simd_f32x4_mul", "simd_f32x4_div", "simd_f32x4_min", "simd_f32x4_max", "simd_f32x4_sqrt",
\t\t"simd_f32x4_ceil", "simd_f32x4_floor", "simd_f32x4_trunc", "simd_f32x4_nearest", "simd_f32x4_demote_f64x2_zero":
\t\treturn 4
\tcase "simd_f64x2_add", "simd_f64x2_sub", "simd_f64x2_mul", "simd_f64x2_div", "simd_f64x2_min", "simd_f64x2_max", "simd_f64x2_sqrt",
\t\t"simd_f64x2_ceil", "simd_f64x2_floor", "simd_f64x2_trunc", "simd_f64x2_nearest", "simd_f64x2_promote_low_f32x4":
\t\treturn 2
\t}
\treturn 0
}

func simdGLaneIsNaN(v [2]uint64, lanes, i int) bool {
\tif lanes == 4 {
\t\tx := uint32(v[i>>1] >> (32 * uint(i&1)))
\t\treturn x&0x7f800000 == 0x7f800000 && x&0x007fffff != 0
\t}
\tx := v[i]
\treturn x&0x7ff0000000000000 == 0x7ff0000000000000 && x&0x000fffffffffffff != 0
}

func simdGEq(got, want [2]uint64, lanes int) bool {
\tif got == want {
\t\treturn true
\t}
\tif lanes == 0 {
\t\treturn false
\t}
\tfor i := 0; i < lanes; i++ {
\t\tvar g, w uint64
\t\tif lanes == 4 {
\t\t\tg = uint64(uint32(got[i>>1] >> (32 * uint(i&1))))
\t\t\tw = uint64(uint32(want[i>>1] >> (32 * uint(i&1))))
\t\t} else {
\t\t\tg, w = got[i], want[i]
\t\t}
\t\tif g == w {
\t\t\tcontinue
\t\t}
\t\tif !simdGLaneIsNaN(got, lanes, i) || !simdGLaneIsNaN(want, lanes, i) {
\t\t\treturn false
\t\t}
\t}
\treturn true
}

"""

TEST_FOOTER = """
func TestGoSIMDMatchesScalar(t *testing.T) {
\tfor name, fns := range simdG_vv_v {
\t\tlanes := simdGNaNLanes(name)
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, b := range simdGCorpus {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), simd_g_from(b))), fns[1](a, b); !simdGEq(got, want, lanes) {
\t\t\t\t\tt.Fatalf("%s(%#x, %#x) = %#x, scalar %#x", name, a, b, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_v_v {
\t\tlanes := simdGNaNLanes(name)
\t\tfor _, a := range simdGCorpus {
\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a))), fns[1](a); !simdGEq(got, want, lanes) {
\t\t\t\tt.Fatalf("%s(%#x) = %#x, scalar %#x", name, a, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vvv_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, b := range simdGCorpus[:12] {
\t\t\t\tfor _, c := range simdGCorpus[:12] {
\t\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), simd_g_from(b), simd_g_from(c))), fns[1](a, b, c); got != want {
\t\t\t\t\t\tt.Fatalf("%s(%#x, %#x, %#x) = %#x, scalar %#x", name, a, b, c, got, want)
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vs_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, s := range simdGShifts {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), s)), fns[1](a, s); got != want {
\t\t\t\t\tt.Fatalf("%s(%#x, %d) = %#x, scalar %#x", name, a, s, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_v_i32 {
\t\tfor _, a := range simdGCorpus {
\t\t\tif got, want := fns[0](simd_g_from(a)), fns[1](a); got != want {
\t\t\t\tt.Fatalf("%s(%#x) = %d, scalar %d", name, a, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_v_i64 {
\t\tfor _, a := range simdGCorpus {
\t\t\tif got, want := fns[0](simd_g_from(a)), fns[1](a); got != want {
\t\t\t\tt.Fatalf("%s(%#x) = %d, scalar %d", name, a, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_v_f32 {
\t\tfor _, a := range simdGCorpus {
\t\t\tif got, want := fns[0](simd_g_from(a)), fns[1](a); math.Float32bits(got) != math.Float32bits(want) {
\t\t\t\tt.Fatalf("%s(%#x) = %v, scalar %v", name, a, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_v_f64 {
\t\tfor _, a := range simdGCorpus {
\t\t\tif got, want := fns[0](simd_g_from(a)), fns[1](a); math.Float64bits(got) != math.Float64bits(want) {
\t\t\t\tt.Fatalf("%s(%#x) = %v, scalar %v", name, a, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vi32_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, x := range simdGScalars32 {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), x)), fns[1](a, x); got != want {
\t\t\t\t\tt.Fatalf("%s(%#x, %d) = %#x, scalar %#x", name, a, x, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vi64_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, x := range simdGScalars64 {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), x)), fns[1](a, x); got != want {
\t\t\t\t\tt.Fatalf("%s(%#x, %d) = %#x, scalar %#x", name, a, x, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vf32_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, x := range simdGFloats32 {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), x)), fns[1](a, x); got != want {
\t\t\t\t\tt.Fatalf("%s(%#x, %v) = %#x, scalar %#x", name, a, x, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_vf64_v {
\t\tfor _, a := range simdGCorpus {
\t\t\tfor _, x := range simdGFloats64 {
\t\t\t\tif got, want := simd_g_to(fns[0](simd_g_from(a), x)), fns[1](a, x); got != want {
\t\t\t\t\tt.Fatalf("%s(%#x, %v) = %#x, scalar %#x", name, a, x, got, want)
\t\t\t\t}
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_s32_v {
\t\tfor _, x := range simdGScalars32 {
\t\t\tif got, want := simd_g_to(fns[0](x)), fns[1](x); got != want {
\t\t\t\tt.Fatalf("%s(%d) = %#x, scalar %#x", name, x, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_s64_v {
\t\tfor _, x := range simdGScalars64 {
\t\t\tif got, want := simd_g_to(fns[0](x)), fns[1](x); got != want {
\t\t\t\tt.Fatalf("%s(%d) = %#x, scalar %#x", name, x, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_f32_v {
\t\tfor _, x := range simdGFloats32 {
\t\t\tif got, want := simd_g_to(fns[0](x)), fns[1](x); got != want {
\t\t\t\tt.Fatalf("%s(%v) = %#x, scalar %#x", name, x, got, want)
\t\t\t}
\t\t}
\t}
\tfor name, fns := range simdG_f64_v {
\t\tfor _, x := range simdGFloats64 {
\t\t\tif got, want := simd_g_to(fns[0](x)), fns[1](x); got != want {
\t\t\t\tt.Fatalf("%s(%v) = %#x, scalar %#x", name, x, got, want)
\t\t\t}
\t\t}
\t}
}

// The constant-pattern shuffle forms must match the generic shuffle for
// every pattern in the corpus, after the emitter's normalization.
func TestGoSIMDShuffleConst(t *testing.T) {
\tfor _, pat := range simdGCorpus {
\t\tvar ia, ib [2]uint64
\t\tsingleA, singleB := true, true
\t\tfor i := 0; i < 16; i++ {
\t\t\tidx := uint8(pat[i>>3] >> (8 * uint(i&7)))
\t\t\tva, vb := uint8(0x80), uint8(0x80)
\t\t\tif idx < 16 {
\t\t\t\tva = idx
\t\t\t\tsingleB = false
\t\t\t} else if idx < 32 {
\t\t\t\tvb = idx - 16
\t\t\t\tsingleA = false
\t\t\t}
\t\t\tia[i>>3] |= uint64(va) << (8 * uint(i&7))
\t\t\tib[i>>3] |= uint64(vb) << (8 * uint(i&7))
\t\t}
\t\tfor _, a := range simdGCorpus[:20] {
\t\t\tfor _, b := range simdGCorpus[:20] {
\t\t\t\twant := simd_i8x16_shuffle(a, b, pat)
\t\t\t\tif got := simd_g_to(simd_g_i8x16_shuffle2(simd_g_from(a), simd_g_from(b), simd_g_from(ia), simd_g_from(ib))); got != want {
\t\t\t\t\tt.Fatalf("shuffle2(%#x, %#x, pat %#x) = %#x, want %#x", a, b, pat, got, want)
\t\t\t\t}
\t\t\t\tif singleA {
\t\t\t\t\tif got := simd_g_to(simd_g_i8x16_swizzle_c(simd_g_from(a), simd_g_from(ia))); got != want {
\t\t\t\t\t\tt.Fatalf("swizzle_c(a) pat %#x = %#x, want %#x", pat, got, want)
\t\t\t\t\t}
\t\t\t\t}
\t\t\t\tif singleB {
\t\t\t\t\tif got := simd_g_to(simd_g_i8x16_swizzle_c(simd_g_from(b), simd_g_from(ib))); got != want {
\t\t\t\t\t\tt.Fatalf("swizzle_c(b) pat %#x = %#x, want %#x", pat, got, want)
\t\t\t\t\t}
\t\t\t\t}
\t\t\t}
\t\t}
\t}
}

func TestGoSIMDLanes(t *testing.T) {
\tfor _, a := range simdGCorpus {
@LANES@
\t}
}

func TestGoSIMDMemory(t *testing.T) {
\tm := memTestModule(t, 256)
\tfor _, addr := range []int32{0, 1, 3, 8, 16, 100, 200, 239, 240, 248, 252, 255} {
\t\tfor _, off := range []int32{0, 1, 16} {
\t\t\tfor _, v := range simdGCorpus[:8] {
@MEM@
\t\t\t}
\t\t}
\t}
}

// simdGTrap runs f and reports whether it trapped (panicked).
func simdGTrap(f func()) (trapped bool) {
\tdefer func() {
\t\tif recover() != nil {
\t\t\ttrapped = true
\t\t}
\t}()
\tf()
\treturn false
}

var nan32 = math.Float32frombits(0x7fc00000)
var nan64 = math.Float64frombits(0x7ff8000000000000)
"""


def gen_test(ops):
    groups = {}
    lane_lines = []
    mem_lines = []
    for name, params, ret, mem in ops:
        g = "simd_g_" + name[len("simd_"):]
        if mem:
            if name.startswith("simd_m64_") or "f16x4" in name or name.endswith("_rng") or name.endswith("_nc"):
                continue
            lanes = [p for p, t in params if p == "lane"]
            if lanes:
                nl = lanes_of(name)
                for L in range(nl):
                    if ret == "[2]uint64":
                        mem_lines.append(
                            "\t\t\t\t{\n\t\t\t\t\tvar got [2]uint64\n\t\t\t\t\tgt := simdGTrap(func() { got = simd_g_to(%s_l%d(m, addr, off, simd_g_from(v))) })\n"
                            "\t\t\t\t\tvar want [2]uint64\n\t\t\t\t\twt := simdGTrap(func() { want = %s(m, addr, off, %d, v) })\n"
                            "\t\t\t\t\tif gt != wt || got != want {\n\t\t\t\t\t\tt.Fatalf(\"%s lane %d addr %%d+%%d: got %%#x trap=%%v, want %%#x trap=%%v\", addr, off, got, gt, want, wt)\n\t\t\t\t\t}\n\t\t\t\t}"
                            % (g, L, name, L, name, L))
                    else:
                        mem_lines.append(
                            "\t\t\t\t{\n\t\t\t\t\tm2 := memTestModule(t, 256)\n\t\t\t\t\tgt := simdGTrap(func() { %s_l%d(m2, addr, off, simd_g_from(v)) })\n"
                            "\t\t\t\t\tm3 := memTestModule(t, 256)\n\t\t\t\t\twt := simdGTrap(func() { %s(m3, addr, off, %d, v) })\n"
                            "\t\t\t\t\tif gt != wt || !bytes.Equal(m2.memory, m3.memory) {\n\t\t\t\t\t\tt.Fatalf(\"%s lane %d addr %%d+%%d: trap=%%v want trap=%%v, memory differs\", addr, off, gt, wt)\n\t\t\t\t\t}\n\t\t\t\t}"
                            % (g, L, name, L, name, L))
            elif ret == "[2]uint64":
                mem_lines.append(
                    "\t\t\t\t{\n\t\t\t\t\tvar got [2]uint64\n\t\t\t\t\tgt := simdGTrap(func() { got = simd_g_to(%s(m, addr, off)) })\n"
                    "\t\t\t\t\tvar want [2]uint64\n\t\t\t\t\twt := simdGTrap(func() { want = %s(m, addr, off) })\n"
                    "\t\t\t\t\tif gt != wt || got != want {\n\t\t\t\t\t\tt.Fatalf(\"%s addr %%d+%%d: got %%#x trap=%%v, want %%#x trap=%%v\", addr, off, got, gt, want, wt)\n\t\t\t\t\t}\n\t\t\t\t}"
                    % (g, name, name))
            else:  # store
                mem_lines.append(
                    "\t\t\t\t{\n\t\t\t\t\tm2 := memTestModule(t, 256)\n\t\t\t\t\tgt := simdGTrap(func() { %s(m2, addr, off, simd_g_from(v)) })\n"
                    "\t\t\t\t\tm3 := memTestModule(t, 256)\n\t\t\t\t\twt := simdGTrap(func() { %s(m3, addr, off, v) })\n"
                    "\t\t\t\t\tif gt != wt || !bytes.Equal(m2.memory, m3.memory) {\n\t\t\t\t\t\tt.Fatalf(\"%s addr %%d+%%d: trap=%%v want trap=%%v, memory differs\", addr, off, gt, wt)\n\t\t\t\t\t}\n\t\t\t\t}"
                    % (g, name, name))
            continue
        if "f16x4" in name:
            continue
        lanes = [p for p, t in params if p == "lane"]
        if lanes:
            nl = lanes_of(name)
            rest = [(p, t) for p, t in params if p != "lane"]
            for L in range(nl):
                if len(rest) == 1:  # extract
                    if ret in ("float32", "float64"):
                        cmpx = "math.%sbits(got) != math.%sbits(want)" % (ret.capitalize(), ret.capitalize())
                    else:
                        cmpx = "got != want"
                    lane_lines.append("\t\tif got, want := %s_l%d(simd_g_from(a)), %s(a, %d); %s {\n\t\t\tt.Fatalf(\"%s lane %d: %%v, scalar %%v\", got, want)\n\t\t}"
                                      % (g, L, name, L, cmpx, name, L))
                else:  # replace
                    xt = rest[1][1]
                    xs = {"int32": "simdGScalars32", "int64": "simdGScalars64", "float32": "simdGFloats32", "float64": "simdGFloats64"}[xt]
                    lane_lines.append("\t\tfor _, x := range %s {\n\t\t\tif got, want := simd_g_to(%s_l%d(simd_g_from(a), x)), %s(a, %d, x); got != want {\n\t\t\t\tt.Fatalf(\"%s lane %d x=%%v: %%#x, scalar %%#x\", x, got, want)\n\t\t\t}\n\t\t}"
                                      % (xs, g, L, name, L, name, L))
            continue
        kinds = tuple(t for _, t in params)
        key = {
            ("[2]uint64", "[2]uint64"): "vv_v", ("[2]uint64",): "v_v", ("[2]uint64", "[2]uint64", "[2]uint64"): "vvv_v",
            ("[2]uint64", "int32"): "vs_v", ("int32",): "s32_v", ("int64",): "s64_v", ("float32",): "f32_v", ("float64",): "f64_v",
        }.get(kinds)
        if key == "v_v" and ret != "[2]uint64":
            key = {"int32": "v_i32", "int64": "v_i64", "float32": "v_f32", "float64": "v_f64"}[ret]
        if key == "vs_v" and ret != "[2]uint64":
            key = None
        assert key is not None, (name, kinds, ret)
        groups.setdefault(key, []).append((name, g))
    sigs = {
        "vv_v": "func(V128, V128) V128, func([2]uint64, [2]uint64) [2]uint64",
        "v_v": "func(V128) V128, func([2]uint64) [2]uint64",
        "vvv_v": "func(V128, V128, V128) V128, func([2]uint64, [2]uint64, [2]uint64) [2]uint64",
        "vs_v": "func(V128, int32) V128, func([2]uint64, int32) [2]uint64",
        "v_i32": "func(V128) int32, func([2]uint64) int32",
        "v_i64": "func(V128) int64, func([2]uint64) int64",
        "v_f32": "func(V128) float32, func([2]uint64) float32",
        "v_f64": "func(V128) float64, func([2]uint64) float64",
        "vi32_v": "func(V128, int32) V128, func([2]uint64, int32) [2]uint64",
        "vi64_v": "func(V128, int64) V128, func([2]uint64, int64) [2]uint64",
        "vf32_v": "func(V128, float32) V128, func([2]uint64, float32) [2]uint64",
        "vf64_v": "func(V128, float64) V128, func([2]uint64, float64) [2]uint64",
        "s32_v": "func(int32) V128, func(int32) [2]uint64",
        "s64_v": "func(int64) V128, func(int64) [2]uint64",
        "f32_v": "func(float32) V128, func(float32) [2]uint64",
        "f64_v": "func(float64) V128, func(float64) [2]uint64",
    }
    out = [TEST_HEADER.replace("@VTAG@", VERSION_TAG).replace('\t"math/rand"\n', '\t"bytes"\n\t"math"\n\t"math/rand"\n')]
    for key, sig in sigs.items():
        a, b = sig.split(", func", 1)
        out.append("var simdG_%s = map[string]struct {\n\tg %s\n\ts func%s\n}{\n" % (key, a, b))
        for name, g in sorted(groups.get(key, [])):
            out.append('\t"%s": {%s, %s},\n' % (name, g, name))
        out.append("}\n\n")
    footer = TEST_FOOTER.replace("@LANES@", "\n".join(lane_lines)).replace("@MEM@", "\n".join(mem_lines))
    # struct field access instead of index
    footer = footer.replace("fns[0]", "fns.g").replace("fns[1]", "fns.s")
    out.append(footer)
    return "".join(out)


def main():
    ops = collect_ops()
    for arch in ("amd64", "arm64"):
        src, stats = gen_arch(arch, ops)
        path = os.path.join(HELPERS, "simd_g_%s_%s.go" % (VERSION, arch))
        with open(path, "w") as f:
            f.write(src)
        print("%s: %d native, %d bridged" % (path, stats["native"], stats["bridge"]))
    path = os.path.join(HELPERS, "simd_g_%s_matrix_test.go" % VERSION)
    with open(path, "w") as f:
        f.write(gen_test(ops))
    print(path)
    subprocess.check_call(["gofmt", "-w", os.path.join(HELPERS, "simd_g_%s_amd64.go" % VERSION),
                           os.path.join(HELPERS, "simd_g_%s_arm64.go" % VERSION), path])


if __name__ == "__main__":
    main()
