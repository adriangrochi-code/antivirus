#!/usr/bin/env bash
# Construye la ISO live/instalable de TISMA OS.
#
#   1. debootstrap de Debian (trixie) en build/chroot
#   2. paquetes (KDE Plasma, Wine/Proton, Waydroid, firmware, seguridad)
#   3. kernel propio (build/kernel-debs, generado por kernel/build-kernel.sh)
#   4. capa del sistema (rootfs/), herramientas centinela/escudo y marca TISMA
#   5. squashfs + GRUB (BIOS y UEFI) → ISO híbrida (USB y DVD)
#
# Variables: BUILD_DIR, DEBIAN_SUITE, DEBIAN_MIRROR, ISO_NAME, SKIP_PACKAGES=1 (pruebas),
#            TISMA_ISO_MINIMA=1 (sin escritorio ni componentes pesados: valida construcción y arranque),
#            TISMA_SB_KEY / TISMA_SB_CERT (clave Secure Boot de TISMA, iso/crear-clave-secureboot.sh: firma el
#            kernel TISMA para arrancar con Secure Boot tras registrar la clave; sin ella, con Secure Boot
#            activado se arranca el kernel firmado de Debian),
#            TISMA_BORRAR_CHROOT=1 (borra el chroot tras crear el squashfs: para discos justos; la build
#            siguiente empieza de cero),
#            TISMA_SIN_COMPILAR=1 (escritorio completo, pero sin compilar componentes externos dentro de la
#            build: hardened_malloc, sched_ext, ML de Escudo; para CI y entornos restringidos),
#            TISMA_ISO_COMPLETA=1 (mete también las apps de todos los perfiles y Strata/REA/Craft: ISO de
#            ~5-6 GB para instalar sin conexión; por defecto se descargan al elegir el perfil o al usarlos),
#            TISMA_BUILD_CA=<archivo.pem> (CA de un proxy HTTPS corporativo: solo durante la build, no va en la ISO)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
BUILD_DIR="${BUILD_DIR:-$REPO/build}"
SUITE="${DEBIAN_SUITE:-trixie}"
MIRROR="${DEBIAN_MIRROR:-https://deb.debian.org/debian}"
CHROOT="$BUILD_DIR/chroot"
ISODIR="$BUILD_DIR/iso"
TISMA_VERSION="$(sed -n 's/^VERSION_ID="\(.*\)"/\1/p' "$REPO/rootfs/usr/lib/os-release")"
ISO_NAME="${ISO_NAME:-tisma-os-${TISMA_VERSION}-preview-$(date +%Y%m%d)-amd64.iso}"

log() { printf '\033[1;35m[iso]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[iso] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ejecutar como root (debootstrap y chroot lo requieren)"
for t in debootstrap mksquashfs xorriso grub-mkimage mkfs.vfat mcopy sbsign rsvg-convert gcc git pip3; do
    command -v "$t" >/dev/null || die "Falta la herramienta: $t (ver README, sección Requisitos)"
done
compgen -G "$BUILD_DIR/kernel-debs/linux-image-*.deb" >/dev/null \
    || die "No hay kernel compilado en $BUILD_DIR/kernel-debs (make kernel)"

in_chroot() { chroot "$CHROOT" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
                  LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"; }

# Paquetes de una lista (packages.list o un perfil de rootfs/usr/share/tisma/perfiles): sin comentarios ni
# componentes «@nombre» (esos se descargan aparte)
lista_paquetes() { sed 's/#.*//' "$@" | tr -s ' \t' '\n' | sed '/^$/d; /^@/d'; }
# Instala los que existan en el repositorio y avisa de los que no
instalar_paquetes() {
    local p ok=()
    for p in "$@"; do
        if in_chroot apt-cache show "$p" >/dev/null 2>&1; then ok+=("$p")
        else log "AVISO: paquete no disponible, se omite: $p"; fi
    done
    log "Instalando ${#ok[@]} paquetes"
    ((${#ok[@]} == 0)) || in_chroot apt-get install -y -q --no-install-recommends "${ok[@]}"
}
PERFILES="$REPO/rootfs/usr/share/tisma/perfiles"

MOUNTS=()
mount_chroot() {
    for fs in proc sys dev dev/pts run; do
        mkdir -p "$CHROOT/$fs"
        mount --bind "/$fs" "$CHROOT/$fs"
        MOUNTS=("$CHROOT/$fs" "${MOUNTS[@]}")
    done
}
umount_chroot() {
    for m in "${MOUNTS[@]}"; do umount -lf "$m" 2>/dev/null || true; done
    MOUNTS=()
}
trap umount_chroot EXIT

# ---------------------------------------------------------------- 1. base
if [[ ! -f "$CHROOT/.base-ok" ]]; then
    log "debootstrap $SUITE"
    rm -rf "$CHROOT"
    # Firmas de Debian OBLIGATORIAS con las claves fijadas en el repositorio (sin --keyring, si el anfitrión no
    # tiene debian-archive-keyring, debootstrap solo avisa y sigue sin verificar)
    debootstrap --arch=amd64 --components=main,contrib,non-free,non-free-firmware \
        --keyring="$REPO/kernel/keys/debian-archive.gpg" --force-check-gpg \
        --include=ca-certificates,curl,gnupg "$SUITE" "$CHROOT" "$MIRROR"
    touch "$CHROOT/.base-ok"
fi
# Sin traducciones ni documentación que no se usan (las licencias se quedan): antes de instalar nada
install -D -m 644 "$REPO/rootfs/etc/dpkg/dpkg.cfg.d/50-tisma-ligero" "$CHROOT/etc/dpkg/dpkg.cfg.d/50-tisma-ligero"
mount_chroot
# Durante la build el chroot resuelve con el DNS del anfitrión. Al final se pone el definitivo (127.0.2.1,
# dnscrypt-proxy), que dentro del chroot no responde: sin esto, reconstruir sobre un chroot ya terminado fallaba.
rm -f "$CHROOT/etc/resolv.conf"
cp -L /etc/resolv.conf "$CHROOT/etc/resolv.conf"

cat > "$CHROOT/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main contrib non-free non-free-firmware
deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware
deb https://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware
deb $MIRROR $SUITE-backports main contrib non-free non-free-firmware
EOF
# Gráficos y firmware de backports (firmados y mantenidos por Debian, con avisos de seguridad): Mesa y el
# firmware de trixie son de abril de 2025 y no reconocen bien las GPU, el Wi-Fi ni el audio más nuevos. Solo
# estos dos orígenes: el resto de backports queda a prioridad 100 (no se instala salvo que se pida).
mkdir -p "$CHROOT/etc/apt/preferences.d"
cat > "$CHROOT/etc/apt/preferences.d/50-tisma-graficos" <<EOF
Explanation: TISMA: Mesa (OpenGL/Vulkan: RADV, ANV, NVK…) y firmware de GPU, Wi-Fi y audio, de backports
Package: src:mesa src:firmware-nonfree
Pin: release n=$SUITE-backports
Pin-Priority: 600

Explanation: kernel firmado por Debian (arranca con Secure Boot sin registrar nada); el de backports, para el
Explanation: hardware más nuevo
Package: src:linux-signed-amd64 src:linux src:linux-base
Pin: release n=$SUITE-backports
Pin-Priority: 600

Explanation: también las versiones i386 (Steam): «src:» solo cubre la arquitectura nativa y Mesa es Multi-Arch
Explanation: «same» (las dos arquitecturas deben tener la misma versión o apt no encuentra solución)
Package: src:mesa:i386
Pin: release n=$SUITE-backports
Pin-Priority: 600
EOF
echo "tisma" > "$CHROOT/etc/hostname"
# Proxy HTTPS que reescribe TLS (empresas, entornos de CI): apt del chroot confía en su CA SOLO durante la build
BUILD_CA_APT="$CHROOT/etc/apt/apt.conf.d/00-tisma-build-ca"
if [[ -n "${TISMA_BUILD_CA:-}" ]]; then
    install -m 644 "$TISMA_BUILD_CA" "$CHROOT/etc/ssl/tisma-build-ca.crt"
    echo 'Acquire::https::CaInfo "/etc/ssl/tisma-build-ca.crt";' > "$BUILD_CA_APT"
fi
# 32 bits (Steam, Wine) solo en la ISO completa; si no, lo añade tisma-perfil al instalar el perfil de juegos
if [[ "${TISMA_ISO_COMPLETA:-0}" == 1 ]]; then in_chroot dpkg --add-architecture i386; fi

# Waydroid (Android) desde su repositorio oficial firmado
# Huella fijada de la clave del repositorio de Waydroid
WAYDROID_FPR="7CE0331F71E0A238BB1002D70E406D181DCEE19C"
# Se exporta SOLO la clave con la huella fijada: «signed-by» confía en todas las claves del archivo, así que un
# archivo con una clave añadida pasaría una simple comprobación con grep.
WGPG="$(mktemp -d)"
if curl -fsSL --retry 3 https://repo.waydro.id/waydroid.gpg -o "$WGPG/descargada.gpg" &&
   gpg --homedir "$WGPG" --batch --quiet --import "$WGPG/descargada.gpg" 2>/dev/null &&
   gpg --homedir "$WGPG" --batch --export "$WAYDROID_FPR" > "$CHROOT/usr/share/keyrings/waydroid.gpg" &&
   [[ "$(gpg --show-keys --with-colons "$CHROOT/usr/share/keyrings/waydroid.gpg" | grep -c '^pub:')" == 1 ]] &&
   gpg --show-keys --with-colons "$CHROOT/usr/share/keyrings/waydroid.gpg" | grep -q "^fpr:::::::::$WAYDROID_FPR:"; then
    echo "deb [signed-by=/usr/share/keyrings/waydroid.gpg] https://repo.waydro.id/ $SUITE main" \
        > "$CHROOT/etc/apt/sources.list.d/waydroid.list"
    WAYDROID=1
else
    rm -f "$CHROOT/usr/share/keyrings/waydroid.gpg"
    log "AVISO: repo.waydro.id inaccesible o clave con huella distinta; la ISO saldrá sin Waydroid"
    WAYDROID=0
fi
rm -rf "$WGPG"
# Un fallo al leer los repositorios debe parar la build (antes solo avisaba y luego se omitían paquetes como
# «no disponibles»). Si falla con Waydroid, que es opcional, se quita su repositorio y se reintenta.
if ! in_chroot apt-get -o APT::Update::Error-Mode=any update -q; then
    if [[ $WAYDROID == 1 ]]; then
        log "AVISO: fallo al leer los repositorios; se reintenta sin Waydroid"
        rm -f "$CHROOT/etc/apt/sources.list.d/waydroid.list"
        WAYDROID=0
        in_chroot apt-get -o APT::Update::Error-Mode=any update -q || die "no se pudieron leer los repositorios de Debian"
    else
        die "no se pudieron leer los repositorios de Debian"
    fi
fi
# Todas las correcciones de seguridad publicadas, también en lo que instaló debootstrap: sin esto, apt, vim y
# demás paquetes base salían con la versión del lanzamiento de trixie aunque trixie-security tuviera el arreglo
# (el sistema instalado se ponía al día después, pero la ISO y la sesión live llevaban fallos conocidos)
in_chroot apt-get -y -q -o Dpkg::Options::=--force-confold full-upgrade || die "no se pudo actualizar el sistema base"

# ---------------------------------------------------------------- 2. paquetes
if [[ "${SKIP_PACKAGES:-0}" != 1 && ! -f "$CHROOT/.packages-ok" ]]; then
    # Licencia de Steam aceptada de antemano (si no, apt se queda esperando)
    in_chroot sh -c 'echo "steam steam/question select I AGREE" | debconf-set-selections
                     echo "steam steam/license note \"\"" | debconf-set-selections
                     echo "locales locales/default_environment_locale select es_ES.UTF-8" | debconf-set-selections
                     echo "locales locales/locales_to_be_generated multiselect es_ES.UTF-8 UTF-8, en_US.UTF-8 UTF-8" | debconf-set-selections'
    LISTA="$HERE/packages.list"
    [[ "${TISMA_ISO_MINIMA:-0}" == 1 ]] && LISTA="$HERE/packages-minima.list"
    mapfile -t wanted < <(lista_paquetes "$LISTA")
    [[ $WAYDROID == 1 ]] && wanted+=(waydroid)
    # TISMA_ISO_COMPLETA=1: también las apps de todos los perfiles (para instalar sin conexión)
    if [[ "${TISMA_ISO_COMPLETA:-0}" == 1 ]]; then mapfile -t -O "${#wanted[@]}" wanted < <(lista_paquetes "$PERFILES"/*.list); fi
    instalar_paquetes "${wanted[@]}"
    # Recomendados solo del escritorio (traducciones, iconos, applets)
    [[ "${TISMA_ISO_MINIMA:-0}" == 1 ]] || in_chroot apt-get install -y -q kde-plasma-desktop
    touch "$CHROOT/.packages-ok"
fi

# ---------------------------------------------------------------- 3. kernel
log "Instalando kernel TISMA"
rm -rf "$CHROOT/tmp/kernel-debs" && mkdir -p "$CHROOT/tmp/kernel-debs"
cp "$BUILD_DIR"/kernel-debs/linux-image-*.deb "$CHROOT/tmp/kernel-debs/"
find "$CHROOT/tmp/kernel-debs" -name '*-dbg_*' -delete
in_chroot sh -c 'dpkg -i /tmp/kernel-debs/*.deb'
# El kernel firmado de Debian (linux-image-amd64, backports) se QUEDA: es el que arranca con Secure Boot (live,
# instalador y sistema instalado hasta registrar la clave TISMA). Antes se purgaba aquí y la ISO acababa
# arrancando el kernel TISMA sin firmar, que el firmware rechaza con Secure Boot activado.
# (La ISO mínima de prueba en QEMU arranca solo con el kernel TISMA.)
# shellcheck disable=SC2016  # ${Status} es formato de dpkg-query, no de bash
if [[ "${TISMA_ISO_MINIMA:-0}" != 1 ]] && ! in_chroot dpkg-query -W -f='${Status}' linux-image-amd64 2>/dev/null \
        | grep -q "ok installed"; then
    in_chroot apt-get install -y -q --no-install-recommends linux-image-amd64
fi
rm -rf "$CHROOT/tmp/kernel-debs"

# ---------------------------------------------------------------- 4. capa del sistema
log "Aplicando capa del sistema y marca TISMA"
# os-release propio (con VERSION_CODENAME): se desvía el de base-files para que una actualización no lo pise
in_chroot dpkg-divert --local --rename --divert /usr/lib/os-release.debian --add /usr/lib/os-release
rsync -a --chown=root:root --exclude __pycache__ "$REPO/rootfs/" "$CHROOT/"
# git no guarda permisos ni carpetas vacías: lo que lo necesita se fija aquí
chmod 600 "$CHROOT"/etc/usbguard/usbguard-daemon.conf "$CHROOT"/etc/usbguard/rules.conf   # usbguard exige 0600
mkdir -p "$CHROOT/etc/centinela"
python3 "$HERE/make-branding.py" "$REPO/branding" "$CHROOT"
# Clic derecho > Crear nuevo: documento de Word, de Excel y de PowerPoint (como en Windows)
python3 "$HERE/plantillas-nuevo.py" "$CHROOT" >/dev/null

# Endurecimiento del sistema base (noviembre de 2026, comparado con Kicksecure/secureblue)
# - Sin el bit setuid/setgid en programas que un escritorio no usa: vía clásica de escalada de privilegios (los
#   medios extraíbles los monta udisks2 con polkit, no mount setuid). dpkg-statoverride: persiste al actualizar.
for f in /usr/bin/chfn /usr/bin/chsh /usr/bin/chage /usr/bin/expiry /usr/bin/gpasswd /usr/bin/newgrp \
         /usr/bin/mount /usr/bin/umount /usr/lib/openssh/ssh-keysign /usr/libexec/lxc/lxc-user-nic; do
    [[ -e "$CHROOT$f" ]] || continue
    in_chroot dpkg-statoverride --list "$f" >/dev/null 2>&1 \
        || in_chroot dpkg-statoverride --update --add root root 0755 "$f"
done
# - 10 contraseñas fallidas seguidas: cuenta bloqueada 10 min (pam_faillock, /etc/security/faillock.conf)
in_chroot pam-auth-update --enable tisma-faillock-pre tisma-faillock-fallo tisma-faillock-ok
# - su solo para administradores (grupo sudo); root sigue pudiendo (pam_rootok va antes)
sed -i 's/^# auth       required   pam_wheel.so$/auth       required   pam_wheel.so group=sudo/' "$CHROOT/etc/pam.d/su"
grep -q '^auth       required   pam_wheel.so group=sudo' "$CHROOT/etc/pam.d/su" || die "no se pudo restringir su"

LIB="$CHROOT/usr/lib/centinela"
mkdir -p "$LIB/python" "$LIB/kernel"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/centinela/centinela" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/escudo/escudo" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/tisma-code/tisma_code" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/asistente/tisma_asistente" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/rendimiento/tisma_rendimiento" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/seguridad/tisma_seguridad_panel" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/guia/tisma_guia" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/bienvenida/tisma_bienvenida" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/actualizar/tisma_actualizar" "$LIB/python/"
# Kit de endurecimiento para Windows XP–8.1 (escudo windows --kit lo copia a C:\TISMA-Kit)
python3 "$REPO/windows/construir-kit.py" "$CHROOT/usr/share/tisma/windows-kit" >/dev/null
# Antes que cualquier apt de la build: su gancho (80-tisma-instantanea) llama a tisma-copias
rsync -a --exclude __pycache__ "$REPO/userspace/sandbox/tisma_sandbox" "$LIB/python/"
rsync -a --exclude __pycache__ "$REPO/userspace/copias/tisma_copias" "$LIB/python/"
rsync -a "$REPO/kernel/" "$LIB/kernel/"
# Herramientas y estrategias de Hope (repo privado): HOPE_SRC = ruta local o URL con acceso
if [[ "${TISMA_ISO_MINIMA:-0}" == 1 ]]; then
    log "Imagen mínima: sin Hope ni componentes pesados"
elif "$REPO/userspace/tisma-code/fetch-hope.sh" "${HOPE_SRC:-https://github.com/adriangrochi-code/hope-0.1}"; then
    rsync -a --delete "$REPO/userspace/tisma-code/vendor/" "$LIB/python/vendor/"
    # Lo que importan sus herramientas (iso/hope-paquetes.list): solo si Hope va en la imagen
    mapfile -t _hope_paq < <(grep -v '^\s*#' "$HERE/hope-paquetes.list" | tr -s ' \t' '\n' | grep -v '^$')
    instalar_paquetes "${_hope_paq[@]}"
else
    log "AVISO: sin acceso a Hope; TISMA Code funcionará sin sus herramientas"
    # Nunca una copia vieja de una build anterior (Hope es privado: una ISO pública no puede llevarlo)
    rm -rf "$LIB/python/vendor"
fi
# Componentes que no están en Debian: sus scripts van también en la imagen para descargarlos al usarlos
# (centinela-apps strata|rea|craft y los «@componente» de los perfiles), con la misma versión y SHA-256 fijados
install -d -m 755 "$CHROOT/usr/lib/tisma/componentes"
for c in strata rea craft-anything openjarvis; do install -m 755 "$HERE/components/$c.sh" "$CHROOT/usr/lib/tisma/componentes/"; done
if [[ "${TISMA_ISO_MINIMA:-0}" != 1 ]]; then
# Tisma engine (laminador) y llama.cpp (motor de TISMA Code en equipos sin GPU dedicada: build oficial, ~36 MB)
"$HERE/components/tisma-engine.sh" "$CHROOT" "${TISMA_ENGINE_VERSION:-}" \
    || log "AVISO: no se pudo instalar Tisma engine"
"$HERE/components/llama-cpp.sh" "$CHROOT" || log "AVISO: no se pudo instalar llama.cpp (TISMA Code usará Strata)"
# Strata (~300 MB, solo útil con GPU dedicada y ≥32 GB), REA (~200 MB) y Craft (~35 MB): se descargan al
# usarlos salvo en la ISO completa
if [[ "${TISMA_ISO_COMPLETA:-0}" == 1 ]]; then
    "$HERE/components/strata.sh" "$CHROOT" || log "AVISO: no se pudo instalar Strata"
    "$HERE/components/rea.sh" "$CHROOT" || log "AVISO: no se pudo instalar REA"
    "$HERE/components/craft-anything.sh" "$CHROOT" || log "AVISO: no se pudo instalar Craft Anything"
fi
if [[ "${TISMA_SIN_COMPILAR:-0}" == 1 ]]; then
    log "TISMA_SIN_COMPILAR=1: sin hardened_malloc, sched_ext, ML de Escudo, ruff ni OpenJarvis"
else
# Herramientas para compilar (perfil «programación»): se quitan después salvo en la ISO completa
mapfile -t compilar < <(lista_paquetes "$PERFILES/programacion.list" | while read -r p; do
    in_chroot dpkg -s "$p" >/dev/null 2>&1 || echo "$p"; done)
instalar_paquetes "${compilar[@]}"
"$HERE/components/hardened-malloc.sh" "$CHROOT" || log "AVISO: no se pudo compilar hardened_malloc"
"$HERE/components/scx.sh" "$CHROOT" || log "AVISO: sin planificadores sched_ext (se usa el del kernel)"
instalar_paquetes python3-scipy python3-sklearn          # lightgbm las necesita (se quedan en la imagen)
"$HERE/components/escudo-ml.sh" "$CHROOT" || log "AVISO: sin motor ML de Escudo (sigue con firmas y YARA)"
"$HERE/components/python-extra.sh" "$CHROOT" || log "AVISO: sin ruff/sounddevice"
# OpenJarvis (opcional, entorno de Python grande): solo en la ISO completa; si no, centinela-apps openjarvis
if [[ "${TISMA_ISO_COMPLETA:-0}" == 1 ]]; then
    "$HERE/components/openjarvis.sh" "$CHROOT" || log "AVISO: no se pudo instalar OpenJarvis (opcional)"
fi
if [[ "${TISMA_ISO_COMPLETA:-0}" != 1 ]] && ((${#compilar[@]})); then
    in_chroot apt-get purge -y -q "${compilar[@]}" || true
    # clang-19 y clang-tools-19 se requieren entre sí y el autoremove no los quitaba (~150 MB con libclang en la
    # preview 5). Ningún programa de la imagen los usa; el perfil «programación» los vuelve a instalar.
    in_chroot apt-get purge -y -q clang-19 clang-tools-19 2>/dev/null || true
    in_chroot apt-get autoremove --purge -y -q || true
fi
fi
fi
# hardened_malloc (asignador endurecido contra desbordamientos del montón en los demonios de red): sin la
# biblioteca, el LD_PRELOAD de sus servicios solo dejaría un error en cada arranque
if [[ ! -e "$CHROOT/usr/lib/libhardened_malloc-light.so" ]]; then
    rm -f "$CHROOT"/etc/systemd/system/*.service.d/60-tisma-hardened-malloc.conf
    log "AVISO: sin hardened_malloc (TISMA_SIN_COMPILAR=1): los demonios de red usan el asignador de glibc"
fi
# Binarios estáticos (funcionan igual en el kernel genérico y en el kernel a medida), con todas las
# protecciones contra desbordamientos: PIE estático (ASLR), canario de pila, FORTIFY, pila protegida contra
# «stack clash», CET (IBT + pila sombra), variables a cero, RELRO completo y pila no ejecutable
ENDURECIDO=(-O2 -static-pie -fPIE -pthread -fstack-protector-strong -fstack-clash-protection -fcf-protection=full
            -D_FORTIFY_SOURCE=3 -ftrivial-auto-var-init=zero "-Wl,-z,relro" "-Wl,-z,now" "-Wl,-z,noexecstack")
gcc "${ENDURECIDO[@]}" -o "$LIB/centinela-membench" "$REPO/userspace/centinela/tools/membench.c"
gcc "${ENDURECIDO[@]}" -o "$LIB/centinela-estres" "$REPO/userspace/centinela/tools/estres.c"
mkdir -p "$CHROOT/var/lib/centinela" "$CHROOT/var/lib/escudo"
# Centinela escribe aquí el ajuste de gráficos para todas las sesiones (con ProtectSystem no podría crearla)
mkdir -p "$CHROOT/etc/environment.d"
chmod 700 "$CHROOT/var/lib/escudo"

# DNS del sistema: dnscrypt-proxy local (cifrado + bloqueo de rastreadores)
install -m 644 "$CHROOT/etc/resolv.conf.tisma" "$CHROOT/etc/resolv.conf.final"
in_chroot systemctl enable dnscrypt-proxy tisma-bloqueo.timer tisma-kernel-actualizar.timer tisma-reinstalar-programas.service auditd 2>/dev/null || true
# Lista de servidores DNS firmada DENTRO de la imagen: sin ella, dnscrypt-proxy necesita descargarla al arrancar y,
# sin red todavía (wifi sin conectar), se cerraba y dejaba el equipo sin DNS (preview 5). Va con su firma
# (minisign_key de dnscrypt-proxy.toml): si alguien la manipulara, dnscrypt-proxy la rechaza.
install -d "$CHROOT/var/cache/dnscrypt-proxy"
for f in public-resolvers.md public-resolvers.md.minisig; do
    curl -fsSL --retry 3 -o "$CHROOT/var/cache/dnscrypt-proxy/$f" \
        "https://download.dnscrypt.info/resolvers-list/v3/$f" || log "AVISO: no se pudo descargar $f"
done
in_chroot chown -R _dnscrypt-proxy: /var/cache/dnscrypt-proxy 2>/dev/null || true
in_chroot systemctl enable systemd-oomd tisma-copias-instantanea.timer tisma-copias-externo.timer 2>/dev/null || true
in_chroot systemctl enable chrony usbguard tisma-bloqueo-sesion tisma-wifi-banda tisma-scx 2>/dev/null || true
in_chroot systemctl enable rasdaemon tisma-salud.timer tisma-scrub.timer tisma-zram-recomprimir.timer 2>/dev/null || true
# Lo que no va en la ISO (LibreOffice…): el instalador lo descarga; si no había red, al primer arranque con red
in_chroot systemctl enable tisma-completar.service 2>/dev/null || true
in_chroot systemctl enable tisma-compartir.path tisma-compartir.service 2>/dev/null || true
in_chroot systemctl enable tisma-drivers.timer 2>/dev/null || true
# Driver gráfico en prueba (GPU AMD GCN 1/2 → amdgpu): confirmar o descartar tras arrancar
in_chroot systemctl enable centinela-graficos.timer 2>/dev/null || true
# Comparaciones A/B: reaplicar los ajustes ganadores y medir cada arranque para comparar kernels
in_chroot systemctl enable centinela-comparar-reaplicar.service centinela-comparar-registrar.timer 2>/dev/null || true
# Apps Flatpak al día con el equipo libre (sustituye al notificador de Discover, desactivado)
in_chroot systemctl enable tisma-flatpak-actualizar.timer 2>/dev/null || true
# /home, /var y /boot sin setuid ni dispositivos
in_chroot systemctl enable tisma-montajes-seguros.service 2>/dev/null || true
# Escudo: reglas YARA y modelos de ML al día (antes nada los descargaba)
in_chroot systemctl enable escudo-actualizar.timer 2>/dev/null || true
chmod 755 "$CHROOT/etc/grub.d/05_tisma_graficos"
# TRIM semanal de todos los SSD (incluido ext4, que ya no usa «discard» síncrono)
in_chroot systemctl enable fstrim.timer 2>/dev/null || log "AVISO: sin fstrim.timer"
# Servicio del análisis dentro de la VM de Escudo (inerte salvo con tisma.detonar en el arranque)
in_chroot systemctl enable tisma-detonar 2>/dev/null || true
# Imagen de la máquina virtual de análisis (make vm-detonacion, ~300 MB ya comprimidos): solo en la ISO completa.
# Sin ella Escudo analiza lo descargado en su sandbox (bubblewrap + seccomp).
rm -rf "$CHROOT/usr/lib/tisma/vm"
if [[ -f "$BUILD_DIR/vm/detonacion.iso" && "${TISMA_ISO_MINIMA:-0}" != 1 && "${TISMA_ISO_COMPLETA:-0}" == 1 ]]; then
    install -D -m 644 -t "$CHROOT/usr/lib/tisma/vm" "$BUILD_DIR"/vm/{detonacion.iso,vmlinuz,initrd.img}
    log "Imagen de análisis incluida (Escudo analizará en máquina virtual si hay KVM)"
fi
# Flathub en Discover desde el primer arranque (apps que Debian no trae o en versiones más nuevas)
if in_chroot sh -c 'command -v flatpak' >/dev/null; then
    in_chroot flatpak remote-add --if-not-exists --system flathub https://dl.flathub.org/repo/flathub.flatpakrepo \
        || log "AVISO: no se pudo añadir Flathub"
fi
in_chroot systemctl disable systemd-timesyncd 2>/dev/null || true
# Nada del arranque espera a que haya red (los servicios que la usan reintentan solos): segundos menos al encender
in_chroot systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
# Esenciales: si alguno no existe se avisa (antes un fallo aquí cortaba la build entera)
for u in nftables apparmor sddm NetworkManager centinela-firstboot tisma-fase2.timer centinela-power \
         centinela-adaptd escudo; do
    # reenable: rehace los enlaces desde la unidad actual (Escudo pasó de la fase 2 a multi-user.target: con
    # «enable» quedaba también el enlace viejo en una imagen reconstruida)
    in_chroot systemctl reenable "$u" 2>/dev/null || log "AVISO: no se pudo habilitar $u"
done
# Arranque escalonado: lo no esencial sale de multi-user.target y lo lanza la fase 2
# (dejarlo en ambos crearía un ciclo de orden con graphical.target)
for u in bluetooth ModemManager clamav-freshclam; do
    in_chroot systemctl disable "$u.service" 2>/dev/null || true
done
in_chroot systemctl enable cups.socket avahi-daemon.socket 2>/dev/null || true
in_chroot systemctl disable cups.service avahi-daemon.service 2>/dev/null || true
if [[ $WAYDROID == 1 ]]; then in_chroot systemctl disable waydroid-container 2>/dev/null || true; fi
in_chroot systemctl set-default graphical.target
# Actualizaciones de seguridad automáticas
cat > "$CHROOT/etc/apt/apt.conf.d/20auto-upgrades" <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
# Lanzador del instalador en el escritorio live (el instalador lo quita del sistema instalado: shellprocess@region).
# Antes se copiaba de install-debian.desktop, que en trixie se llama calamares-install-debian.desktop: la preview 5
# no tenía icono para instalar. Ejecutable: si no, Plasma pregunta si abrirlo o ejecutarlo.
# Nombres simples en el menú y la barra: el explorador se llama «Archivos» (copia en /usr/local, que tiene prioridad;
# las actualizaciones del paquete no la pisan).
if [[ -f "$CHROOT/usr/share/applications/org.kde.dolphin.desktop" ]]; then
    install -d "$CHROOT/usr/local/share/applications"
    sed -E -e '/^\[Desktop Action/,$b' -e 's/^Name(\[[^]]*\])?=.*/Name\1=Archivos/' \
        "$CHROOT/usr/share/applications/org.kde.dolphin.desktop" > "$CHROOT/usr/local/share/applications/org.kde.dolphin.desktop"
fi
# Menú limpio: sin lanzadores técnicos o repetidos y con nombres claros (iso/lanzadores.py)
python3 "$HERE/lanzadores.py" "$CHROOT" >/dev/null
# Sin carpeta «Plantillas» en la carpeta personal: las plantillas de «Crear nuevo» viven en /usr/share/templates.
if [[ -f "$CHROOT/etc/xdg/user-dirs.defaults" ]]; then
    sed -i 's/^TEMPLATES=/#TEMPLATES=/' "$CHROOT/etc/xdg/user-dirs.defaults"
fi
# En «Escritorio», no «Desktop»: si existe ~/Desktop al crear el usuario, xdg-user-dirs lo adopta y queda una carpeta en
# inglés entre Documentos, Descargas, Imágenes… (visto en la VM)
install -D -m 755 "$CHROOT/usr/share/tisma/live/instalar-tisma.desktop" "$CHROOT/etc/skel/Escritorio/instalar-tisma.desktop"
# Todo lo que el instalador y la sesión del USB ejecutan de verdad. La build instala sin «recomendados» y así
# faltaron user-setup (sin usuario en el USB: pantalla negra), dosfstools y squashfs-tools (la instalación fallaba)
# y cryptsetup-initramfs (un sistema cifrado no arrancaba). Si falta algo, la ISO no se genera.
for orden in calamares calamares-install-debian unsquashfs rsync mkfs.fat mkfs.ext4 mkfs.btrfs cryptsetup sfdisk \
             grub-install efibootmgr os-prober blkid mkswap update-initramfs locale-gen useradd chpasswd sddm kwin_wayland; do
    in_chroot sh -c "command -v $orden" >/dev/null || die "falta «$orden» en la imagen (el instalador fallaría)"
done
for f in /usr/lib/user-setup/user-setup-apply /usr/share/initramfs-tools/hooks/cryptroot \
         /usr/share/polkit-1/actions/com.github.calamares.calamares.policy; do
    [[ -e "$CHROOT$f" ]] || die "falta $f en la imagen"
done
# El instalador se abre solo en la sesión del USB y sin pedir contraseña; si el arranque se queda sin pantalla de
# inicio, el vigía muestra por qué
in_chroot systemctl enable tisma-live-instalador.service tisma-vigia-arranque.service 2>/dev/null || true

# Secure Boot: el kernel TISMA se firma con la clave propia (MOK) si se proporciona; su certificado va en el
# sistema para registrarlo con «tisma-secureboot activar». Sin clave, con Secure Boot arranca el de Debian.
if [[ -n "${TISMA_SB_KEY:-}" && -n "${TISMA_SB_CERT:-}" ]]; then
    for k in "$CHROOT"/boot/vmlinuz-*-centinela-*; do
        sbsign --key "$TISMA_SB_KEY" --cert "$TISMA_SB_CERT" --output "$k.firmado" "$k" && mv "$k.firmado" "$k"
        log "Kernel TISMA firmado para Secure Boot: $(basename "$k")"
    done
    install -d "$CHROOT/usr/share/tisma/secureboot"
    openssl x509 -in "$TISMA_SB_CERT" -outform DER -out "$CHROOT/usr/share/tisma/secureboot/tisma-mok.der"
else
    log "Sin TISMA_SB_KEY: el kernel TISMA no se firma (con Secure Boot activado arrancará el de Debian)"
fi
in_chroot systemctl enable tisma-secureboot.service 2>/dev/null || true
# Pantalla de arranque con el logo (el tema lo genera make-branding.py a partir de «spinner»)
in_chroot sh -c 'command -v plymouth-set-default-theme >/dev/null && [ -d /usr/share/plymouth/themes/tisma ] && plymouth-set-default-theme tisma' \
    || log "AVISO: sin tema de arranque TISMA"
in_chroot update-initramfs -u -k all
# Iconos de las apps de TISMA recién generados: caché al día (las apps GTK no los verían si no)
in_chroot sh -c 'command -v gtk-update-icon-cache >/dev/null && gtk-update-icon-cache -q -f /usr/share/icons/hicolor' || true
in_chroot apt-get clean
# Cachés del sistema (ldconfig, catálogo del registro, hwdb) ya al día: sin estas marcas systemd las regeneraba en
# CADA arranque del USB (ldconfig: 88 s en QEMU) porque /usr parecía más nuevo que /etc
touch "$CHROOT/etc/.updated" "$CHROOT/var/.updated"
rm -f "$BUILD_CA_APT" "$CHROOT/etc/ssl/tisma-build-ca.crt"      # la CA de la build nunca va en la imagen
rm -rf "$CHROOT"/var/lib/apt/lists/* "$CHROOT"/tmp/* "$CHROOT"/root/.bash_history
# resolv.conf definitivo al final (durante la build el chroot usa el DNS del anfitrión)
mv -f "$CHROOT/etc/resolv.conf.final" "$CHROOT/etc/resolv.conf"
umount_chroot

# Binarios firmados de arranque (del chroot: los mismos paquetes de Debian que llevará el sistema instalado)
SBROOT="$BUILD_DIR/secureboot"
rm -rf "$SBROOT" && mkdir -p "$SBROOT/usr/lib/shim" "$SBROOT/usr/lib/grub"
cp -a "$CHROOT/usr/lib/shim/." "$SBROOT/usr/lib/shim/"
cp -a "$CHROOT/usr/lib/grub/x86_64-efi-signed" "$SBROOT/usr/lib/grub/"

# ---------------------------------------------------------------- 5. ISO
log "Creando squashfs (${TISMA_COMPRESION:-xz})"
rm -rf "$ISODIR" && mkdir -p "$ISODIR/live" "$ISODIR/boot/grub"
# xz con filtro x86 (por defecto): ~8 % menos que zstd -19, para que la ISO quepa en una release de GitHub
# (< 2 GiB por archivo). La sesión live descomprime algo más lento; el sistema instalado no se ve afectado.
# TISMA_COMPRESION=zstd: más rápida en la sesión live, ISO más grande.
if [[ "${TISMA_COMPRESION:-xz}" == zstd ]]; then COMP=(-comp zstd -Xcompression-level 19)
else COMP=(-comp xz -Xbcj x86 -Xdict-size 100%); fi
mksquashfs "$CHROOT" "$ISODIR/live/filesystem.squashfs" "${COMP[@]}" \
    -b 1M -noappend -e boot/efi -e .base-ok -e .packages-ok -quiet
# Sesión live e instalador con el kernel FIRMADO de Debian: arranca con Secure Boot en cualquier PC y trae todos
# los drivers. El kernel TISMA, como entrada aparte (con Secure Boot, solo si está firmado y su clave registrada).
KVER="$(find "$CHROOT/boot" -maxdepth 1 -name 'vmlinuz-*' ! -name '*centinela*' -printf '%f\n' | sort -V | tail -n1 | sed 's/^vmlinuz-//')"
TVER="$(find "$CHROOT/boot" -maxdepth 1 -name 'vmlinuz-*centinela*' -printf '%f\n' | sort -V | tail -n1 | sed 's/^vmlinuz-//')"
[[ -n "$KVER" ]] || KVER="$TVER"
cp "$CHROOT/boot/vmlinuz-$KVER" "$ISODIR/live/vmlinuz"
cp "$CHROOT/boot/initrd.img-$KVER" "$ISODIR/live/initrd.img"
if [[ -n "$TVER" && "$TVER" != "$KVER" ]]; then
    cp "$CHROOT/boot/vmlinuz-$TVER" "$ISODIR/live/vmlinuz-tisma"
    cp "$CHROOT/boot/initrd.img-$TVER" "$ISODIR/live/initrd-tisma.img"
fi
log "Kernel de la sesión live: $KVER${TVER:+ · kernel TISMA: $TVER}"
cp -r "$CHROOT/boot/grub/themes" "$ISODIR/boot/grub/"

LIVE="boot=live components quiet splash locales=es_ES.UTF-8 keyboard-layouts=es timezone=Europe/Madrid hostname=tisma username=tisma user-fullname=TISMA"
# Los MISMOS parámetros de seguridad que el sistema instalado (una sola fuente: 50-centinela.cfg), más los de
# 32 bits (la sesión live no trae el perfil de juegos). Antes la live tenía su propia lista y le faltaban varios.
SEC="$(sed -n 's/^GRUB_CMDLINE_LINUX_DEFAULT="quiet splash \(.*\)"$/\1/p' "$REPO/rootfs/etc/default/grub.d/50-centinela.cfg")"
[[ "$SEC" == *lockdown=integrity* ]] || die "no se pudieron leer los parámetros de seguridad de 50-centinela.cfg"
SEC="$SEC ia32_emulation=0 vdso32=0"
# Menú del USB como el DVD de Windows: no se ve; arranca directo al instalador. Si el equipo YA tiene TISMA instalado
# (marca /tisma-instalado en su partición de arranque: el USB se quedó puesto tras instalar), arranca el sistema
# instalado. Esc durante los 3 s muestra las opciones (probar sin instalar, gráficos básicos, diagnóstico…).
# El arranque silencioso ya viene en SEC (50-centinela.cfg); el modo diagnóstico lo quita para ver los mensajes
SILENCIO="loglevel=3 rd.udev.log_level=3 systemd.show_status=false vt.global_cursor_default=0"
[[ "$SEC" == *"$SILENCIO"* ]] || die "50-centinela.cfg ya no trae los parámetros de arranque silencioso"
DIAG="${LIVE/ quiet splash/} ${SEC/$SILENCIO /}"
cat > "$ISODIR/boot/grub/grub.cfg" <<FINMENU
insmod all_video
insmod gfxterm
insmod png
set gfxmode=auto
terminal_output gfxterm
# Fuentes del tema: con Secure Boot GRUB prohíbe cargarlas («prohibited by secure boot policy», visto en la VM)
# y usa la suya, que no tiene acentos: por eso las opciones van sin tildes. Sin Secure Boot, DejaVu.
if [ "\$lockdown" != "y" ]; then
    loadfont /boot/grub/themes/centinela/dejavu-regular-16.pf2
    loadfont /boot/grub/themes/centinela/dejavu-bold-16.pf2
fi
set theme=/boot/grub/themes/centinela/theme.txt
set timeout_style=hidden
set timeout=3
set default=instalar
# Con TISMA ya instalado: como los DVD de instalación, unos segundos para elegir arrancar desde el USB (instalar,
# actualizar o formatear); si no se toca nada, arranca el sistema instalado (el USB olvidado puesto). Antes
# arrancaba el instalado SIN avisar y no había forma visible de llegar a actualizar (visto en la A275).
# GRUB solo deja interrumpir la espera con Esc. Sin tildes: con Secure Boot la fuente de GRUB no las tiene.
if search --no-floppy --set=instalado --file /tisma-instalado; then
    menuentry "Arrancar TISMA OS (instalado en este equipo)" --id disco {
        set root=\$instalado
        configfile /grub/grub.cfg
    }
    echo ""
    echo "    Pulsa Esc para arrancar desde el USB (instalar o actualizar TISMA)..."
    if sleep --interruptible 6; then
        set default=disco
        set timeout=0
    else
        set default=instalar
        set timeout=0
    fi
fi
menuentry "Instalar TISMA OS" --id instalar {
    linux /live/vmlinuz $LIVE $SEC
    initrd /live/initrd.img
}
menuentry "Probar TISMA OS sin instalar" {
    linux /live/vmlinuz $LIVE $SEC tisma.probar
    initrd /live/initrd.img
}
menuentry "Instalar TISMA OS con video simple (si la pantalla queda en negro)" {
    linux /live/vmlinuz $LIVE $SEC nomodeset
    initrd /live/initrd.img
}
menuentry "Probar TISMA OS desde la memoria RAM (requiere 8 GB)" {
    linux /live/vmlinuz $LIVE $SEC toram tisma.probar
    initrd /live/initrd.img
}
menuentry "Modo de soporte: ver los mensajes del arranque (consola en Ctrl+Alt+F9)" {
    linux /live/vmlinuz $DIAG tisma.diagnostico systemd.debug_shell=tty9
    initrd /live/initrd.img
}
FINMENU
if [[ -f "$ISODIR/live/vmlinuz-tisma" ]]; then
    cat >> "$ISODIR/boot/grub/grub.cfg" <<FINMENU
menuentry "Probar TISMA OS con el kernel TISMA (requiere Secure Boot desactivado o su clave registrada)" {
    linux /live/vmlinuz-tisma $LIVE $SEC tisma.probar
    initrd /live/initrd-tisma.img
}
FINMENU
fi
if [[ "${TISMA_BORRAR_CHROOT:-0}" == 1 ]]; then
    # Solo si no queda NADA montado dentro (si no, borrar el chroot destrozaría /proc, /dev… del anfitrión)
    if grep -q " $CHROOT/" /proc/mounts; then
        log "AVISO: el chroot aún tiene montajes; no se borra"
    else
        rm -rf "$CHROOT"
        log "Chroot borrado para dejar sitio a la ISO (TISMA_BORRAR_CHROOT=1)"
    fi
fi
log "Generando ISO híbrida BIOS + UEFI con Secure Boot (shim de Microsoft → GRUB y kernel firmados por Debian)"
"$HERE/secureboot-iso.sh" "$ISODIR" "$SBROOT" "$BUILD_DIR/$ISO_NAME" TISMA_OS
( cd "$BUILD_DIR" && sha256sum "$ISO_NAME" > "$ISO_NAME.sha256" )
log "Listo: $BUILD_DIR/$ISO_NAME ($(du -h "$BUILD_DIR/$ISO_NAME" | cut -f1))"
