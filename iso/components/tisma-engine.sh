#!/usr/bin/env bash
# Instala Tisma engine (laminador) en /opt/tisma dentro del chroot de la ISO.
# 1º intenta el binario publicado en GitHub; si no existe, compila desde el código.
# Uso: tisma-engine.sh <chroot> [versión|main]
set -euo pipefail
CHROOT="$1"
REF="${2:-latest}"
REPO_URL="https://github.com/adriangrochi-code/tisma_engine"
DEST="$CHROOT/opt/tisma"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[tisma-engine]\033[0m %s\n' "$*"; }

if [[ "$REF" == latest ]]; then
    REF="$(curl -fsSL "https://raw.githubusercontent.com/adriangrochi-code/tisma_engine/main/VERSION" || echo main)"
fi

installed=0
if [[ "$REF" != main ]]; then
    asset="tisma-engine-$REF-linux-x64.zip"
    if curl -fsSL --retry 3 -o "$WORK/$asset" "$REPO_URL/releases/download/v$REF/$asset"; then
        log "Usando la release v$REF"
        (cd "$WORK" && python3 -m zipfile -e "$asset" out)
        rm -rf "$DEST" && mkdir -p "$DEST"
        cp -a "$WORK"/out/*/. "$DEST/"
        installed=1
    fi
fi

if [[ $installed == 0 ]]; then
    log "Sin release descargable: compilando desde el código ($REF)"
    branch="main"
    [[ "$REF" == main ]] || branch="v$REF"
    git clone -q --depth 1 --branch "$branch" "$REPO_URL" "$WORK/src"
    cmake -S "$WORK/src" -B "$WORK/build" -G Ninja -DCMAKE_BUILD_TYPE=Release
    cmake --build "$WORK/build" --parallel
    rm -rf "$DEST" && mkdir -p "$DEST/docs"
    cp "$WORK/build/tisma-engine" "$WORK/build/apps/tisma/tisma" "$DEST/"
    cp "$WORK"/src/{LICENSE,README.md,CHANGELOG.md,THIRD_PARTY.md,VERSION} "$DEST/"
fi
chmod 755 "$DEST"/tisma "$DEST"/tisma-engine
mkdir -p "$CHROOT/usr/bin"
ln -sf /opt/tisma/tisma "$CHROOT/usr/bin/tisma"
ln -sf /opt/tisma/tisma-engine "$CHROOT/usr/bin/tisma-engine"
if [[ -x "$DEST/solidcraft/solidcraft-cli" ]]; then
    ln -sf /opt/tisma/solidcraft/solidcraft-cli "$CHROOT/usr/bin/solidcraft-cli"
fi
mkdir -p "$CHROOT/usr/share/applications"
cat > "$CHROOT/usr/share/applications/net.tisma.Tisma.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Tisma
GenericName=Laminador de impresión 3D
Comment=Laminar, analizar y diseñar piezas para impresión 3D
Exec=tisma %f
Icon=tisma
Terminal=false
Categories=Graphics;3DGraphics;Engineering;
MimeType=model/stl;model/3mf;application/vnd.ms-3mfdocument;model/obj;text/x-gcode;
Keywords=slicer;3d;stl;gcode;impresora;
DESK
log "Instalado en /opt/tisma (licencia PolyForm Noncommercial: ver /opt/tisma/LICENSE)"
