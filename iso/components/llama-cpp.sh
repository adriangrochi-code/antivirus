#!/usr/bin/env bash
# llama.cpp (MIT, github.com/ggml-org/llama.cpp): motor de inferencia que usan TISMA Code y sus especialistas
# (llama-server). Debian trixie no lo empaqueta. Se compila dentro del chroot a una versión FIJADA:
# - CPU: todas las variantes (SSE4.2, AVX, AVX2/FMA, AVX-512…) cargadas al ejecutar según la CPU real, en vez
#   de -march=native de la máquina de compilación.
# - Vulkan: aceleración en GPU integradas y dedicadas (AMD/Intel/NVIDIA con Mesa o su driver).
# Uso: llama-cpp.sh <chroot>
set -euo pipefail
CHROOT="$1"
TAG="${LLAMA_TAG:-b12282}"
COMMIT="${LLAMA_COMMIT:-aa75ff3239f9cc675b081697deda9e1b112345c6}"
log() { printf '\033[1;35m[llama.cpp]\033[0m %s\n' "$*"; }
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
