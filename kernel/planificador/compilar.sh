#!/usr/bin/env bash
# Compila scx_tisma dentro del árbol oficial de sched-ext/scx (versión fijada por commit: usa sus bibliotecas
# scx_utils/scx_stats y sus cabeceras BPF). Deja el binario en <BUILD_DIR>/scx/target/release/scx_tisma.
# Requiere: rustup (Rust ≥ 1.91), clang ≥ 20 (con clang 18 el verificador del kernel rechaza el BPF),
#           libelf, zlib, libzstd y pkg-config.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-$HERE/../../build}"
SCX_TAG=v1.1.4
SCX_COMMIT=e7b3a219617472cf78880ac424714e52c932db61
SRC="$BUILD_DIR/scx"

if [[ ! -d "$SRC/.git" ]]; then
    git clone --depth 1 --branch "$SCX_TAG" https://github.com/sched-ext/scx.git "$SRC"
fi
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$SCX_COMMIT" ]] || {
    echo "scx en $SRC no es $SCX_TAG ($SCX_COMMIT): no se compila" >&2; exit 1; }

rm -rf "$SRC/scheds/rust/scx_tisma"
cp -r "$HERE/scx_tisma" "$SRC/scheds/rust/scx_tisma"
grep -q '"scheds/rust/scx_tisma"' "$SRC/Cargo.toml" ||
    sed -i 's|^\(\s*\)"scheds/rust/scx_bpfland",|&\n\1"scheds/rust/scx_tisma",|' "$SRC/Cargo.toml"

CLANG="${BPF_CLANG:-}"
if [[ -z "$CLANG" ]]; then
    for c in clang-22 clang-21 clang-20 clang; do command -v "$c" >/dev/null && { CLANG=$c; break; }; done
fi
ver=$("$CLANG" -dumpversion | cut -d. -f1)
(( ver >= 20 )) || { echo "hace falta clang ≥ 20 para el BPF (hay $CLANG $ver)" >&2; exit 1; }
[[ -d "$HOME/.cargo/bin" ]] && PATH="$HOME/.cargo/bin:$PATH"
(cd "$SRC" && BPF_CLANG="$CLANG" cargo build --release -p scx_tisma)
ls -la "$SRC/target/release/scx_tisma"
