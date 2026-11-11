#!/usr/bin/env bash
# Planificadores sched_ext (scx_lavd para escritorio/juegos, scx_bpfland para servidores), GPL-2.0,
# github.com/sched-ext/scx. Debian trixie no los empaqueta. Versión FIJADA v1.0.14: las posteriores exigen
# Rust 1.91 y trixie trae 1.85. Se compila dentro del chroot con Cargo --locked (dependencias de Cargo.lock).
# Si falla, la imagen sale sin ellos y el kernel usa su planificador normal (EEVDF).
# Uso: scx.sh <chroot>
set -euo pipefail
CHROOT="$1"
TAG="v1.0.14"
COMMIT="2e3fb1d48f6b80578b2b35e67cdc0cf10549fc94"
log() { printf '\033[1;35m[scx]\033[0m %s\n' "$*"; }
SRC="$CHROOT/usr/src/scx"
rm -rf "$SRC"
git clone -q --depth 1 --branch "$TAG" https://github.com/sched-ext/scx.git "$SRC"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$COMMIT" ]] || { log "la etiqueta $TAG no apunta al commit fijado"; exit 1; }
chroot "$CHROOT" /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root CARGO_HOME=/usr/src/scx/.cargo \
    sh -c "cd /usr/src/scx && cargo build --release --locked -p scx_lavd -p scx_bpfland"
for b in scx_lavd scx_bpfland; do
    install -m 755 "$SRC/target/release/$b" "$CHROOT/usr/local/bin/$b"
done
install -D -m 644 "$SRC/LICENSE" "$CHROOT/usr/share/doc/scx/LICENSE"
rm -rf "$SRC"
log "scx_lavd y scx_bpfland instalados ($TAG)"
