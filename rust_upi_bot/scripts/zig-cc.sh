#!/bin/sh
# zig cc wrapper cho cross-compile C deps (BoringSSL, zstd...) sang musl.
# zig ship sẵn sysroot musl đầy đủ (CRT + libc) → fix thiếu Scrt1.o/libgcc.
#
# cc-rs/cmake truyền clang-triple 4 phần `aarch64-unknown-linux-musl`, nhưng
# zig parser chỉ nhận `aarch64-linux-musl` (3 phần). Dịch `-unknown-` → `-`.
for a in "$@"; do
  case "$a" in
    --target=*)
      a="--target=$(printf '%s' "${a#--target=}" | sed 's/-unknown-/-/')"
      ;;
  esac
  if [ -z "${_zig_set:-}" ]; then
    set -- "$a"
    _zig_set=1
  else
    set -- "$@" "$a"
  fi
done
exec zig cc "$@"
