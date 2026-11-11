#!/usr/bin/env bash
# Motor de aprendizaje automático de Escudo (EMBER2024): bibliotecas que Debian trixie no empaqueta.
# - lightgbm, signify, mscerts: ruedas de PyPI con SHA-256 FIJADO (pip --require-hashes).
# - Extractor de características de EMBER2024 (thrember/features.py, Apache-2.0) a un commit fijado. Solo se
#   instala features.py: el resto del paquete (entrenamiento) arrastra polars, matplotlib y huggingface_hub.
# El resto de dependencias vienen de Debian (python3-numpy, -sklearn, -pefile, -asn1crypto, -oscrypto,
# -certvalidator, -typing-extensions; libgomp1). Uso: escudo-ml.sh <chroot>
set -euo pipefail
CHROOT="$1"
DEST="$CHROOT/usr/lib/centinela/python"
EMBER_COMMIT="${EMBER_COMMIT:-0ef753e81d98bf209f71b03cd331dfc190b5b54d}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
log() { printf '\033[1;35m[escudo-ml]\033[0m %s\n' "$*"; }

cat > "$WORK/req.txt" <<'REQ'
lightgbm==4.7.0 --hash=sha256:d23e922acd891e77212e4d0fbcee9ba973c96dee479491341d05ba595357ebb7
signify==0.9.3 --hash=sha256:c67f327e34e480e18ff7c4d56f48703dc40e12bd77a4cccfebc0fd0e1fd4e3da
mscerts==2026.10.27 --hash=sha256:7152052d6b23bbfe23bf26844dc9c2da00299dfb1f48c62889743b6396bcfaab
REQ
log "Descargando ruedas verificadas"
python3 -m pip download -q --no-deps --require-hashes --only-binary=:all: \
    --platform manylinux_2_28_x86_64 --python-version 3.13 -r "$WORK/req.txt" -d "$WORK/whl"
python3 -m pip install -q --no-deps --no-index --no-compile --target "$DEST" "$WORK"/whl/*.whl

log "Extractor de características EMBER2024 ($EMBER_COMMIT)"
git clone -q https://github.com/FutureComputing4AI/EMBER2024.git "$WORK/ember"
git -C "$WORK/ember" checkout -q "$EMBER_COMMIT"
[[ "$(git -C "$WORK/ember" rev-parse HEAD)" == "$EMBER_COMMIT" ]] || { log "commit inesperado"; exit 1; }
mkdir -p "$DEST/thrember"
install -m 644 "$WORK/ember/src/thrember/features.py" "$WORK/ember/src/thrember/pefile_warnings.txt" "$DEST/thrember/"
printf '"""EMBER2024 (thrember): solo el extractor de características, usado por Escudo."""\n' > "$DEST/thrember/__init__.py"
install -D -m 644 "$WORK/ember/LICENSE" "$CHROOT/usr/share/doc/thrember/LICENSE"
log "Instalado en /usr/lib/centinela/python"
