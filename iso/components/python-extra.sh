#!/usr/bin/env bash
# Herramientas de Python que Debian trixie no empaqueta, con versión y SHA-256 FIJADOS (pip --require-hashes):
# - ruff (MIT): linter y formateador que usa el arnés de TISMA Code.
# - sounddevice (MIT): micrófono para el asistente (detección de doble palmada); usa libportaudio2 de Debian.
# Uso: python-extra.sh <chroot>
set -euo pipefail
CHROOT="$1"
DEST="$CHROOT/usr/lib/centinela/python"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cat > "$WORK/req.txt" <<'REQ'
ruff==0.17.4 --hash=sha256:2cd025509b2e15721b426fc70fd946d71f43b3db3b08e946dfec2d696d911690
sounddevice==0.5.6 --hash=sha256:de099612311ad81e55d31ccbd83f43ea6bf4d87b48f9b6ea55a1fbcde0eee4e0
REQ
python3 -m pip download -q --no-deps --require-hashes --only-binary=:all: \
    --platform manylinux_2_17_x86_64 --platform any --python-version 3.13 -r "$WORK/req.txt" -d "$WORK/whl"
python3 -m pip install -q --no-deps --no-index --no-compile --target "$DEST" "$WORK"/whl/*.whl
ln -sf /usr/lib/centinela/python/bin/ruff "$CHROOT/usr/local/bin/ruff"
printf '\033[1;35m[python-extra]\033[0m ruff y sounddevice instalados\n'
