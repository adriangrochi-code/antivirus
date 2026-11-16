#!/usr/bin/env bash
# Compila Cloud Hypervisor (VMM en Rust) en la versión fijada por commit: no está en Debian.
# Deja cloud-hypervisor y ch-remote en <BUILD_DIR>/cloud-hypervisor/target/release/.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="${BUILD_DIR:-$HERE/../../build}"
CH_TAG=v54.1
CH_COMMIT=74aeae19cb1ee2c44d33796b0e4e95e1a68e94dd
SRC="$BUILD_DIR/cloud-hypervisor"
[[ -d "$SRC/.git" ]] || git clone --depth 1 --branch "$CH_TAG" https://github.com/cloud-hypervisor/cloud-hypervisor.git "$SRC"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$CH_COMMIT" ]] || { echo "no es $CH_TAG ($CH_COMMIT): no se compila" >&2; exit 1; }
[[ -d "$HOME/.cargo/bin" ]] && PATH="$HOME/.cargo/bin:$PATH"
# --locked: exactamente las dependencias de su Cargo.lock
(cd "$SRC" && cargo build --release --locked --bin cloud-hypervisor --bin ch-remote)
ls -la "$SRC/target/release/cloud-hypervisor" "$SRC/target/release/ch-remote"
