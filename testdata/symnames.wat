;; Symbol-name fixture: a handful of named functions, some of which
;; call each other. symnames2.wat is the same module with an extra
;; import and an extra function inserted in front, which shifts every
;; function index by two; with -symbol-names the generated names and
;; chunk assignments of the common functions must not change.
(module
  (import "env" "log" (func $log (param i32)))
  (memory (export "memory") 1)
  (func $square (param i32) (result i32)
    local.get 0
    local.get 0
    i32.mul)
  (func $twice (param i32) (result i32)
    local.get 0
    i32.const 2
    i32.mul)
  (func $sum_of_squares (param i32 i32) (result i32)
    local.get 0
    call $square
    local.get 1
    call $square
    i32.add)
  (func $report (param i32)
    local.get 0
    call $twice
    call $log)
  (func $store_at (param i32 i32)
    local.get 0
    local.get 1
    i32.store offset=65536)
  (export "sum_of_squares" (func $sum_of_squares))
  (export "report" (func $report))
  (export "store_at" (func $store_at))
  (export "twice" (func $twice)))
