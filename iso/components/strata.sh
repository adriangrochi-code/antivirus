#!/usr/bin/env bash
# Instala Strata (motor de inferencia de IA local, MIT, github.com/Niko1221/Strata)
# en /opt/strata dentro del chroot. Se verifica con el SHA256SUMS.txt publicado.
# El motor para la GPU y los modelos se descargan al usarlo (no van en la ISO).
# Uso: strata.sh <chroot>
set -euo pipefail
CHROOT="$1"
BASE="https://github.com/Niko1221/Strata/releases/latest/download"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[strata]\033[0m %s\n' "$*"; }

curl -fsSL --retry 3 -o "$WORK/SHA256SUMS.txt" "$BASE/SHA256SUMS.txt"
line="$(grep -E '  Strata-[0-9.]+-linux-x86_64\.AppImage$' "$WORK/SHA256SUMS.txt" | head -n1)"
[[ -n "$line" ]] || { log "No hay AppImage de Linux en la última release"; exit 1; }
file="${line##* }"
log "Descargando $file"
curl -fsSL --retry 3 -o "$WORK/$file" "$BASE/$file"
(cd "$WORK" && echo "$line" | sha256sum -c -) || { log "Checksum incorrecto: descarga descartada"; exit 1; }

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
