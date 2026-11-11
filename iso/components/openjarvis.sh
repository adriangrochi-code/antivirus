#!/usr/bin/env bash
# OpenJarvis (Stanford, Apache-2.0) en /opt/openjarvis, en un commit fijado y SIN el instalador
# oficial (que mete Ollama, curl|sh y procesos en segundo plano). Opcional: si falla, la ISO sigue.
# Uso: openjarvis.sh <chroot>
set -euo pipefail
CHROOT="$1"
COMMIT="68d4290663984508db94a6a59ade45c2b17859ed"
UV_VERSION="0.13.7"
log() { printf '\033[1;35m[openjarvis]\033[0m %s\n' "$*"; }
in_chroot() { chroot "$CHROOT" /usr/bin/env -i HOME=/root PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 "$@"; }

rm -rf "$CHROOT/opt/openjarvis"
git init -q "$CHROOT/opt/openjarvis"
git -C "$CHROOT/opt/openjarvis" fetch -q --depth 1 https://github.com/open-jarvis/OpenJarvis "$COMMIT"
git -C "$CHROOT/opt/openjarvis" checkout -q FETCH_HEAD
[[ "$(git -C "$CHROOT/opt/openjarvis" rev-parse HEAD)" == "$COMMIT" ]] || { log "commit inesperado"; exit 1; }
rm -rf "$CHROOT/opt/openjarvis/.git" "$CHROOT/opt/openjarvis/scripts/install"
in_chroot python3 -m venv /opt/openjarvis/.herramientas
# Herramientas de construcción con versión y SHA-256 fijados (antes: uv sin hash y «uvx maturin» = la última)
cat > "$CHROOT/opt/openjarvis/.herramientas/req.txt" <<REQ
uv==$UV_VERSION --hash=sha256:2c9bdc4a872b60a93c5f26c0791089ea11585a4f9d3b3c017d78eb8487505e25
maturin==1.16.0 --hash=sha256:837db2f4d93d4899c3ac393fb3cc9eebf35b6002336e5d574f4a4fb6c49fe283
REQ
in_chroot /opt/openjarvis/.herramientas/bin/pip install -q --no-deps --only-binary=:all: --require-hashes \
    -r /opt/openjarvis/.herramientas/req.txt
# uv.lock fija versiones y hashes de todas las dependencias (--frozen: no se resuelve nada nuevo)
in_chroot sh -c "cd /opt/openjarvis && /opt/openjarvis/.herramientas/bin/uv sync --frozen --extra server --extra speech"
# --locked: las dependencias de Rust exactamente las de Cargo.lock
in_chroot sh -c "cd /opt/openjarvis && VIRTUAL_ENV=/opt/openjarvis/.venv PATH=/opt/openjarvis/.herramientas/bin:\$PATH maturin develop --uv --locked \
    -m rust/crates/openjarvis-python/Cargo.toml --release"
install -D -m 644 "$(dirname "$0")/../../rootfs/usr/share/tisma/openjarvis/config.toml" \
    "$CHROOT/usr/share/tisma/openjarvis/config.toml"
log "OpenJarvis instalado en /opt/openjarvis ($COMMIT)"
