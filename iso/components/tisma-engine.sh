#!/usr/bin/env bash
# Instala Tisma engine (laminador) en /opt/tisma dentro del chroot de la ISO.
# Por defecto: la release FIJADA (versión + SHA-256). Si la descarga falla o el hash no coincide, se aborta
# (nunca se cae a compilar otra cosa). Solo con «main», pedido explícitamente, se compila desde el código,
# dentro del chroot (no como root en la máquina de build).
# Uso: tisma-engine.sh <chroot> [versión|main]
set -euo pipefail
CHROOT="$1"
REF="${2:-0.12.0-beta.1}"
# SHA-256 fijado del zip de la release (actualizar junto con la versión)
TISMA_ENGINE_SHA256="${TISMA_ENGINE_SHA256:-e0ec94346cf4160fc8a5b68967dc131418e261c775a8f604a83fd56066dacd5f}"
REPO_URL="https://github.com/adriangrochi-code/tisma_engine"
DEST="$CHROOT/opt/tisma"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[tisma-engine]\033[0m %s\n' "$*"; }


installed=0
if [[ "$REF" != main ]]; then
    asset="tisma-engine-$REF-linux-x64.zip"
    curl -fsSL --retry 3 -o "$WORK/$asset" "$REPO_URL/releases/download/v$REF/$asset" \
        || { log "No se pudo descargar la release v$REF"; exit 1; }
    echo "$TISMA_ENGINE_SHA256  $WORK/$asset" | sha256sum -c - >/dev/null \
        || { log "SHA-256 incorrecto: descarga descartada"; exit 1; }
    log "Usando la release v$REF"
    (cd "$WORK" && python3 -m zipfile -e "$asset" out)
    rm -rf "$DEST" && mkdir -p "$DEST"
    cp -a "$WORK"/out/*/. "$DEST/"
    installed=1
fi

if [[ $installed == 0 ]]; then
    log "Compilando la rama main dentro del chroot (pedido explícito; sin versión fijada)"
    SRC="$CHROOT/usr/src/tisma_engine"
    rm -rf "$SRC"
    git clone -q --depth 1 --branch main "$REPO_URL" "$SRC"
    log "Commit: $(git -C "$SRC" rev-parse HEAD)"
    chroot "$CHROOT" /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root \
        cmake -S /usr/src/tisma_engine -B /usr/src/tisma_engine/build -G Ninja -DCMAKE_BUILD_TYPE=Release
    chroot "$CHROOT" /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root \
        cmake --build /usr/src/tisma_engine/build --parallel
    rm -rf "$DEST" && mkdir -p "$DEST/docs"
    cp "$SRC/build/tisma-engine" "$SRC/build/apps/tisma/tisma" "$DEST/"
    cp "$SRC"/{LICENSE,README.md,CHANGELOG.md,THIRD_PARTY.md,VERSION} "$DEST/"
    rm -rf "$SRC"
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
