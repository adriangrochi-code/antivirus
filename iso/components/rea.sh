#!/usr/bin/env bash
# REA (Reverse Engineer Anything, MIT) para el arnés de TISMA Code.
# Node.js oficial con SHA-256 fijado + rea-agents con integridad SHA-512 fijada, SIN scripts de
# instalación (--ignore-scripts: nada de postinstall de frida ni descargas ocultas).
# Uso: rea.sh <chroot>
set -euo pipefail
CHROOT="$1"
NODE_VERSION="22.23.4"
NODE_SHA256="1805bcd35e6815559bd2eb97db438d3fdb0eb3a5e6a977b7c1badca76337e9ce"
REA_VERSION="6.8.0"
REA_INTEGRITY="sha512-9QDeRnGqEDzABZyr04eLnBXBv/c6I9/TWu/6M0MD4pEfkySI/BIgwTODIbAReX3C1x6hjw9+Jlx4A2yUSPZ9ww=="
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[rea]\033[0m %s\n' "$*"; }

tarball="node-v$NODE_VERSION-linux-x64.tar.xz"
curl -fsSL --retry 3 -o "$WORK/$tarball" "https://nodejs.org/dist/v$NODE_VERSION/$tarball"
echo "$NODE_SHA256  $WORK/$tarball" | sha256sum -c - >/dev/null || { log "SHA-256 de Node incorrecto"; exit 1; }
mkdir -p "$CHROOT/opt/node"
tar -C "$CHROOT/opt/node" --strip-components=1 -xJf "$WORK/$tarball"

# Comprobar la integridad publicada en el registro contra la fijada antes de instalar
got="$("$CHROOT/opt/node/bin/node" "$CHROOT/opt/node/lib/node_modules/npm/bin/npm-cli.js" view \
       "rea-agents@$REA_VERSION" dist.integrity 2>/dev/null)"
[[ "$got" == "$REA_INTEGRITY" ]] || { log "Integridad de rea-agents distinta de la fijada"; exit 1; }
chroot "$CHROOT" /opt/node/bin/npm install --global --prefix /usr --ignore-scripts --no-audit --no-fund \
    "rea-agents@$REA_VERSION"
ln -sf /opt/node/bin/node "$CHROOT/usr/local/bin/node"
log "REA $REA_VERSION instalado (Node $NODE_VERSION)"
