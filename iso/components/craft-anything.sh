#!/usr/bin/env bash
# Instala el motor de Craft Anything (MIT o Apache-2.0, github.com/storytold/craft-anything) en
# /opt/craft-anything dentro del chroot. Solo la CLI/servidor MCP que usa TISMA Code (aislado con
# bubblewrap); no la app de escritorio ni sus capas de IA en la nube.
# Uso: craft-anything.sh <chroot>
set -euo pipefail
CHROOT="$1"
# Versión y SHA-256 FIJADOS en el repositorio (comprobados contra el SHA256SUMS.txt de la release).
# Para actualizar: cambiar ambos valores tras verificar la nueva release.
CRAFT_VERSION="${CRAFT_VERSION:-0.6.0}"
CRAFT_SHA256="${CRAFT_SHA256:-cbc65972a8eb4c89ed74b99bcf14aa5ea0f7aa9da709def38da79ccacc5e07a0}"
BASE="https://github.com/storytold/craft-anything/releases/download/v$CRAFT_VERSION"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[craft]\033[0m %s\n' "$*"; }

file="craft-anything-$CRAFT_VERSION-linux-x86_64.tar.gz"
dir="craft-anything-$CRAFT_VERSION-linux-x86_64"
log "Descargando $file"
curl -fsSL --retry 3 -o "$WORK/$file" "$BASE/$file"
echo "$CRAFT_SHA256  $WORK/$file" | sha256sum -c - >/dev/null || { log "SHA-256 incorrecto: descarga descartada"; exit 1; }

tar -xzf "$WORK/$file" -C "$WORK" --no-same-owner --no-same-permissions \
    "$dir/bin/craft-anything-cli" "$dir/share/doc/craft-anything"
mkdir -p "$CHROOT/opt/craft-anything/bin" "$CHROOT/usr/share/doc/craft-anything"
install -m 755 "$WORK/$dir/bin/craft-anything-cli" "$CHROOT/opt/craft-anything/bin/craft-anything-cli"
for f in LICENSE-MIT LICENSE-APACHE NOTICE README.md; do
    install -m 644 "$WORK/$dir/share/doc/craft-anything/$f" "$CHROOT/usr/share/doc/craft-anything/$f"
done
echo "$CRAFT_VERSION" > "$CHROOT/opt/craft-anything/VERSION"
log "Instalado: /opt/craft-anything ($CRAFT_VERSION)"
