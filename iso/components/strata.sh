#!/usr/bin/env bash
# Instala Strata (motor de inferencia de IA local, MIT, github.com/Niko1221/Strata)
# en /opt/strata dentro del chroot. Se verifica con el SHA256SUMS.txt publicado.
# El motor para la GPU y los modelos se descargan al usarlo (no van en la ISO).
# Uso: strata.sh <chroot>
set -euo pipefail
CHROOT="$1"
# Versión y SHA-256 FIJADOS en el repositorio (no se confía en el checksum del propio servidor).
# Para actualizar: cambiar ambos valores tras verificar la nueva release.
STRATA_VERSION="${STRATA_VERSION:-0.2.13}"
STRATA_SHA256="${STRATA_SHA256:-5284e3c1f6270695c8cf8a60c97e686495d52995a9d5ff5dc4a94bbd33adeec9}"
BASE="https://github.com/Niko1221/Strata/releases/download/v$STRATA_VERSION"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[strata]\033[0m %s\n' "$*"; }

file="Strata-$STRATA_VERSION-linux-x86_64.AppImage"
log "Descargando $file"
curl -fsSL --retry 3 -o "$WORK/$file" "$BASE/$file"
echo "$STRATA_SHA256  $WORK/$file" | sha256sum -c - >/dev/null || { log "SHA-256 incorrecto: descarga descartada"; exit 1; }

mkdir -p "$CHROOT/opt/strata" "$CHROOT/usr/bin" "$CHROOT/usr/share/applications"
install -m 755 "$WORK/$file" "$CHROOT/opt/strata/Strata.AppImage"
ver="${file#Strata-}"; echo "${ver%%-linux*}" > "$CHROOT/opt/strata/VERSION"
ln -sf /opt/strata/Strata.AppImage "$CHROOT/usr/bin/strata-app"
cat > "$CHROOT/usr/share/applications/strata.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Strata
GenericName=IA local
Comment=Modelos de lenguaje en tu propio equipo, sin enviar datos a internet
Exec=/opt/strata/Strata.AppImage
Icon=applications-science
Terminal=false
Categories=Development;Science;Utility;
Keywords=ia;ai;llm;chat;qwen;local;
DESK
log "Instalado: /opt/strata ($(cat "$CHROOT/opt/strata/VERSION"))"
