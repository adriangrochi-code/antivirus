#!/usr/bin/env bash
# shellcheck disable=SC2094
# Compila el kernel de Centinela OS a partir de fragmentos de configuración.
#
# Perfiles:
#   minimal   x86_64_defconfig + kvm_guest: máquinas virtuales, compilación rápida
#   generic   config de Debian (BASE_CONFIG) + fragmentos: hardware real, la ISO
#   tailored  config base + localmodconfig + fragmento de hardware generado por
#             `centinela plan`: kernel optimizado para UNA máquina concreta
#
# Variables: KERNEL_VERSION, KERNEL_PROFILE, BUILD_DIR, BASE_CONFIG,
#            HW_FRAGMENT (tailored), LSMOD_FILE (tailored), JOBS, LOCALVERSION
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KERNEL_VERSION="${KERNEL_VERSION:-6.18.63}"
KERNEL_PROFILE="${KERNEL_PROFILE:-generic}"
BUILD_DIR="${BUILD_DIR:-$HERE/../build}"
JOBS="${JOBS:-$(nproc)}"
LOCALVERSION="${LOCALVERSION:--centinela-$KERNEL_PROFILE}"
MAJOR="${KERNEL_VERSION%%.*}"
SRC="$BUILD_DIR/linux-$KERNEL_VERSION"
TARBALL="$BUILD_DIR/linux-$KERNEL_VERSION.tar.xz"
URL="https://cdn.kernel.org/pub/linux/kernel/v$MAJOR.x"

log() { printf '\033[1;35m[kernel]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[kernel] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

mkdir -p "$BUILD_DIR"

# ---------------------------------------------------------------- fuente
if [[ ! -d "$SRC" ]]; then
    if [[ ! -f "$TARBALL" ]]; then
        log "Descargando linux-$KERNEL_VERSION"
        curl -fL --retry 4 -o "$TARBALL.part" "$URL/linux-$KERNEL_VERSION.tar.xz"
        mv "$TARBALL.part" "$TARBALL"
    fi
    log "Verificando suma SHA-256 publicada por kernel.org"
    curl -fsSL --retry 4 -o "$BUILD_DIR/sha256sums.asc" "$URL/sha256sums.asc"
    if command -v gpg >/dev/null && [[ "${VERIFY_GPG:-0}" == 1 ]]; then
        gpg --verify "$BUILD_DIR/sha256sums.asc" || die "Firma GPG inválida"
    fi
    expected="$(awk -v f="linux-$KERNEL_VERSION.tar.xz" '$2==f {print $1}' "$BUILD_DIR/sha256sums.asc")"
    [[ -n "$expected" ]] || die "No hay checksum publicado para linux-$KERNEL_VERSION"
    echo "$expected  $TARBALL" | sha256sum -c - || die "Checksum incorrecto: tarball corrupto o manipulado"
    log "Extrayendo"
    tar -C "$BUILD_DIR" -xf "$TARBALL"
fi

cd "$SRC"
FRAGMENTS=("$HERE"/fragments/*.config)

# ---------------------------------------------------------------- config base
case "$KERNEL_PROFILE" in
    minimal)
        make -s x86_64_defconfig
        make -s kvm_guest.config
        ;;
    generic)
        [[ -f "${BASE_CONFIG:-}" ]] || die "Perfil generic requiere BASE_CONFIG (p. ej. /boot/config-* de Debian)"
        cp "$BASE_CONFIG" .config
        ;;
    tailored)
        base="${BASE_CONFIG:-/boot/config-$(uname -r)}"
        [[ -f "$base" ]] || die "No existe la config base $base"
        [[ -f "${HW_FRAGMENT:-}" ]] || die "Perfil tailored requiere HW_FRAGMENT (centinela plan)"
        cp "$base" .config
        make -s olddefconfig
        # Solo los módulos del hardware presente (lsmod capturado tras usar el equipo).
        lsmod_file="${LSMOD_FILE:-}"
        if [[ -z "$lsmod_file" ]]; then lsmod_file="$(mktemp)"; lsmod > "$lsmod_file"; fi
        yes '' | make -s LSMOD="$lsmod_file" localmodconfig
        FRAGMENTS+=("$HW_FRAGMENT")
        ;;
    *) die "Perfil desconocido: $KERNEL_PROFILE" ;;
esac

# ---------------------------------------------------------------- fragmentos
log "Aplicando fragmentos: ${FRAGMENTS[*]##*/}"
# Debian firma con una clave propia que no tenemos: se genera una nueva por build.
scripts/config --set-str SYSTEM_TRUSTED_KEYS "" --set-str SYSTEM_REVOCATION_KEYS "" \
               --set-str MODULE_SIG_KEY "certs/signing_key.pem" --set-str LOCALVERSION ""
KCONFIG_CONFIG=.config scripts/kconfig/merge_config.sh -m -O . .config "${FRAGMENTS[@]}" >/dev/null

# Drivers de arranque (disco raíz + sistema de archivos) integrados en el kernel:
# se busca en los Makefile qué símbolo CONFIG_ compila cada módulo.
if [[ -f "${BUILTIN_MODULES:-}" ]]; then
    while IFS= read -r mod; do
        [[ -n "$mod" ]] || continue
        for name in "$mod" "${mod//_/-}"; do
            sym="$(grep -rhoE --include=Makefile "obj-\\\$\\((CONFIG_[A-Z0-9_]+)\\)\s*[+:]?=\s*$name\.o" drivers fs 2>/dev/null \
                   | head -n1 | sed -E 's/.*\((CONFIG_[A-Z0-9_]+)\).*/\1/')"
            if [[ -n "$sym" ]]; then
                log "Integrando $mod ($sym=y)"
                scripts/config --enable "${sym#CONFIG_}"
                break
            fi
        done
    done < "$BUILTIN_MODULES"
fi
make -s olddefconfig

# ---------------------------------------------------------------- verificación
log "Verificando que cada opción quedó aplicada"
missing=0
for frag in "${FRAGMENTS[@]}"; do
    while IFS= read -r line; do
        [[ "$line" =~ ^(CONFIG_[A-Z0-9_]+)=(.*)$ ]] || [[ "$line" =~ ^#\ (CONFIG_[A-Z0-9_]+)\ is\ not\ set$ ]] || continue
        sym="${BASH_REMATCH[1]}"
        if [[ "$line" == \#* ]]; then
            grep -q "^${sym}=[ym]" .config && { echo "  ! $sym debería estar desactivada ($(basename "$frag"))"; missing=$((missing+1)); }
        else
            grep -qxF "$line" .config || { echo "  ! $line -> $(grep -E "^(# )?${sym}[= ]" .config || echo 'no disponible') ($(basename "$frag"))"; missing=$((missing+1)); }
        fi
    done < "$frag"
done
log "$missing opciones no aplicadas (dependencias o no soportadas en esta arquitectura)"
[[ "${STRICT_CONFIG:-0}" == 1 && $missing -gt 0 ]] && die "STRICT_CONFIG=1 y hay opciones sin aplicar"

# ---------------------------------------------------------------- compilación
log "Compilando con $JOBS hilos (perfil $KERNEL_PROFILE)"
make -j"$JOBS" LOCALVERSION="$LOCALVERSION" KDEB_PKGVERSION="$KERNEL_VERSION-1" \
     bindeb-pkg 2>&1 | tail -n 20
mkdir -p "$BUILD_DIR/kernel-debs"
mv "$BUILD_DIR"/linux-*"$KERNEL_VERSION"*.deb "$BUILD_DIR/kernel-debs/" 2>/dev/null || true
mv "$BUILD_DIR"/linux-upstream* "$BUILD_DIR/kernel-debs/" 2>/dev/null || true
cp .config "$BUILD_DIR/kernel-debs/config-$KERNEL_VERSION$LOCALVERSION"
log "Paquetes listos en $BUILD_DIR/kernel-debs"
ls -1 "$BUILD_DIR/kernel-debs"
