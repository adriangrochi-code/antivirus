#!/usr/bin/env bash
# llama.cpp (MIT, github.com/ggml-org/llama.cpp): motor de inferencia que usan TISMA Code y sus especialistas
# (llama-server). Debian trixie no lo empaqueta.
# Por defecto se instala la build OFICIAL publicada en GitHub (Vulkan + todas las variantes de CPU: SSE4.2,
# AVX, AVX2/FMA —Haswell, Piledriver/Excavator como el A275—, AVX-512…, cargadas al ejecutar según la CPU
# real), con SHA-256 FIJADO aquí: ~36 MB, sin compilar. Sus dependencias (libvulkan1, libgomp1, libssl3,
# libstdc++) ya están en la base; glibc ≥ 2.34.
# LLAMA_COMPILAR=1: compilarlo dentro del chroot desde el commit fijado (necesita cmake, ninja, glslc…).
# Uso: llama-cpp.sh <chroot>
set -euo pipefail
CHROOT="$1"
TAG="${LLAMA_TAG:-b12282}"
COMMIT="${LLAMA_COMMIT:-aa75ff3239f9cc675b081697deda9e1b112345c6}"
BIN_SHA256="${LLAMA_BIN_SHA256:-d7aef9b8316b1f1faf45a79a337a55bb0aaf3a5a7b2a08527d3030fdfb4c228a}"
log() { printf '\033[1;35m[llama.cpp]\033[0m %s\n' "$*"; }

if [[ "${LLAMA_COMPILAR:-0}" != 1 ]]; then
    WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
    file="llama-$TAG-bin-ubuntu-vulkan-x64.tar.gz"
    log "Descargando $file"
    curl -fsSL --retry 3 -o "$WORK/$file" "https://github.com/ggml-org/llama.cpp/releases/download/$TAG/$file"
    echo "$BIN_SHA256  $WORK/$file" | sha256sum -c - >/dev/null || { log "SHA-256 incorrecto: descarga descartada"; exit 1; }
    tar -xzf "$WORK/$file" -C "$WORK" --no-same-owner --no-same-permissions
    # Todo en un directorio: los binarios buscan sus bibliotecas junto a ellos (RUNPATH $ORIGIN)
    rm -rf "$CHROOT/opt/llama.cpp"
    mkdir -p "$CHROOT/opt/llama.cpp/bin" "$CHROOT/usr/local/bin"
    cp -a "$WORK/llama-$TAG/." "$CHROOT/opt/llama.cpp/bin/"
    chmod -R u=rwX,go=rX "$CHROOT/opt/llama.cpp"
    for b in llama-server llama-cli llama-bench llama-quantize; do
        if [[ -x "$CHROOT/opt/llama.cpp/bin/$b" ]]; then ln -sf "/opt/llama.cpp/bin/$b" "$CHROOT/usr/local/bin/$b"; fi
    done
    install -D -m 644 "$WORK/llama-$TAG/LICENSE" "$CHROOT/usr/share/doc/llama.cpp/LICENSE"
    rm -f "$CHROOT/etc/ld.so.conf.d/llama-cpp.conf"
    echo "$TAG binario-oficial $BIN_SHA256" > "$CHROOT/opt/llama.cpp/VERSION"
    log "Instalado en /opt/llama.cpp ($TAG, build oficial)"
    exit 0
fi

SRC="$CHROOT/usr/src/llama.cpp"
rm -rf "$SRC"
git clone -q --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git "$SRC"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$COMMIT" ]] || { log "la etiqueta $TAG no apunta al commit fijado"; exit 1; }
run() { chroot "$CHROOT" /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root "$@"; }
log "Compilando $TAG (CPU multivariante + Vulkan)"
run cmake -S /usr/src/llama.cpp -B /usr/src/llama.cpp/build -G Ninja -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_INSTALL_PREFIX=/opt/llama.cpp -DGGML_NATIVE=OFF -DGGML_BACKEND_DL=ON -DGGML_CPU_ALL_VARIANTS=ON \
    -DGGML_VULKAN=ON -DLLAMA_CURL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DBUILD_SHARED_LIBS=ON
run cmake --build /usr/src/llama.cpp/build -j"$(nproc)"
run cmake --install /usr/src/llama.cpp/build
for b in llama-server llama-cli llama-bench llama-quantize; do
    if [[ -x "$CHROOT/opt/llama.cpp/bin/$b" ]]; then ln -sf "/opt/llama.cpp/bin/$b" "$CHROOT/usr/local/bin/$b"; fi
done
echo "/opt/llama.cpp/lib" > "$CHROOT/etc/ld.so.conf.d/llama-cpp.conf"
run ldconfig
install -D -m 644 "$SRC/LICENSE" "$CHROOT/usr/share/doc/llama.cpp/LICENSE"
echo "$TAG $COMMIT" > "$CHROOT/opt/llama.cpp/VERSION"
rm -rf "$SRC"
log "Instalado en /opt/llama.cpp ($TAG)"
