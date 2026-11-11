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
in_chroot /opt/openjarvis/.herramientas/bin/pip install -q "uv==$UV_VERSION"
# uv.lock fija versiones y hashes de todas las dependencias (--frozen: no se resuelve nada nuevo)
in_chroot sh -c "cd /opt/openjarvis && /opt/openjarvis/.herramientas/bin/uv sync --frozen --extra server --extra speech"
in_chroot sh -c "cd /opt/openjarvis && /opt/openjarvis/.herramientas/bin/uvx maturin develop --uv \
    -m rust/crates/openjarvis-python/Cargo.toml --release"
install -D -m 644 "$(dirname "$0")/../../rootfs/usr/share/tisma/openjarvis/config.toml" \
    "$CHROOT/usr/share/tisma/openjarvis/config.toml"
log "OpenJarvis instalado en /opt/openjarvis ($COMMIT)"
