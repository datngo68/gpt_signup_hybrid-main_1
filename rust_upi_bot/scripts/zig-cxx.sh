#!/bin/sh
# zig c++ wrapper cho cross-compile C++ deps (BoringSSL) sang musl.
# Dịch clang-triple `aarch64-unknown-linux-musl` → zig `aarch64-linux-musl`.
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
exec zig c++ "$@"
