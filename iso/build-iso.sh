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
#            TISMA_ISO_MINIMA=1 (sin escritorio ni componentes pesados: valida construcción y arranque)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
BUILD_DIR="${BUILD_DIR:-$REPO/build}"
SUITE="${DEBIAN_SUITE:-trixie}"
MIRROR="${DEBIAN_MIRROR:-https://deb.debian.org/debian}"
CHROOT="$BUILD_DIR/chroot"
ISODIR="$BUILD_DIR/iso"
ISO_NAME="${ISO_NAME:-tisma-os-$(date +%Y%m%d)-amd64.iso}"

log() { printf '\033[1;35m[iso]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[iso] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "Ejecutar como root (debootstrap y chroot lo requieren)"
for t in debootstrap mksquashfs xorriso grub-mkrescue rsvg-convert gcc git pip3; do
    command -v "$t" >/dev/null || die "Falta la herramienta: $t (ver README, sección Requisitos)"
done
compgen -G "$BUILD_DIR/kernel-debs/linux-image-*.deb" >/dev/null \
    || die "No hay kernel compilado en $BUILD_DIR/kernel-debs (make kernel)"

in_chroot() { chroot "$CHROOT" /usr/bin/env -i HOME=/root PATH=/usr/sbin:/usr/bin:/sbin:/bin \
                  LANG=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"; }

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
mount_chroot

cat > "$CHROOT/etc/apt/sources.list" <<EOF
deb $MIRROR $SUITE main contrib non-free non-free-firmware
deb $MIRROR $SUITE-updates main contrib non-free non-free-firmware
deb https://security.debian.org/debian-security $SUITE-security main contrib non-free non-free-firmware
EOF
echo "tisma" > "$CHROOT/etc/hostname"
in_chroot dpkg --add-architecture i386

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
in_chroot apt-get update -q

# ---------------------------------------------------------------- 2. paquetes
if [[ "${SKIP_PACKAGES:-0}" != 1 && ! -f "$CHROOT/.packages-ok" ]]; then
    # Licencia de Steam aceptada de antemano (si no, apt se queda esperando)
    in_chroot sh -c 'echo "steam steam/question select I AGREE" | debconf-set-selections
                     echo "steam steam/license note \"\"" | debconf-set-selections
                     echo "locales locales/default_environment_locale select es_ES.UTF-8" | debconf-set-selections
                     echo "locales locales/locales_to_be_generated multiselect es_ES.UTF-8 UTF-8, en_US.UTF-8 UTF-8" | debconf-set-selections'
    LISTA="$HERE/packages.list"
    [[ "${TISMA_ISO_MINIMA:-0}" == 1 ]] && LISTA="$HERE/packages-minima.list"
    mapfile -t wanted < <(sed 's/#.*//' "$LISTA" | tr -s ' \t' '\n' | sed '/^$/d')
    [[ $WAYDROID == 1 ]] && wanted+=(waydroid)
    pkgs=()
    for p in "${wanted[@]}"; do
        if in_chroot apt-cache show "$p" >/dev/null 2>&1; then pkgs+=("$p")
        else log "AVISO: paquete no disponible, se omite: $p"; fi
    done
    log "Instalando ${#pkgs[@]} paquetes"
    in_chroot apt-get install -y -q --no-install-recommends "${pkgs[@]}"
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
# Fuera los kernels genéricos de Debian si alguno entró como dependencia
# shellcheck disable=SC2016  # ${Package} es formato de dpkg-query, no de bash
mapfile -t debian_kernels < <(in_chroot dpkg-query -W -f='${Package}\n' 'linux-image-*' 2>/dev/null \
                               | grep -v centinela | grep -v -- '-dbg' || true)
if ((${#debian_kernels[@]})); then in_chroot apt-get purge -y -q "${debian_kernels[@]}"; fi
rm -rf "$CHROOT/tmp/kernel-debs"

# ---------------------------------------------------------------- 4. capa del sistema
log "Aplicando capa del sistema y marca TISMA"
# os-release propio (con VERSION_CODENAME): se desvía el de base-files para que una actualización no lo pise
in_chroot dpkg-divert --local --rename --divert /usr/lib/os-release.debian --add /usr/lib/os-release
rsync -a --chown=root:root "$REPO/rootfs/" "$CHROOT/"
# git no guarda permisos ni carpetas vacías: lo que lo necesita se fija aquí
chmod 600 "$CHROOT"/etc/usbguard/usbguard-daemon.conf "$CHROOT"/etc/usbguard/rules.conf   # usbguard exige 0600
mkdir -p "$CHROOT/etc/centinela"
python3 "$HERE/make-branding.py" "$REPO/branding" "$CHROOT"

LIB="$CHROOT/usr/lib/centinela"
mkdir -p "$LIB/python" "$LIB/kernel"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/centinela/centinela" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/escudo/escudo" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/tisma-code/tisma_code" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/asistente/tisma_asistente" "$LIB/python/"
rsync -a --delete --exclude __pycache__ "$REPO/userspace/rendimiento/tisma_rendimiento" "$LIB/python/"
rsync -a "$REPO/kernel/" "$LIB/kernel/"
# Herramientas y estrategias de Hope (repo privado): HOPE_SRC = ruta local o URL con acceso
if [[ "${TISMA_ISO_MINIMA:-0}" == 1 ]]; then
    log "Imagen mínima: sin Hope ni componentes pesados"
elif "$REPO/userspace/tisma-code/fetch-hope.sh" "${HOPE_SRC:-https://github.com/adriangrochi-code/hope-0.1}"; then
    rsync -a --delete "$REPO/userspace/tisma-code/vendor/" "$LIB/python/vendor/"
else
    log "AVISO: sin acceso a Hope; TISMA Code funcionará sin sus herramientas"
fi
if [[ "${TISMA_ISO_MINIMA:-0}" != 1 ]]; then
# Aplicaciones: Tisma engine (laminador) y Strata (IA local)
"$HERE/components/tisma-engine.sh" "$CHROOT" "${TISMA_ENGINE_VERSION:-}" \
    || log "AVISO: no se pudo instalar Tisma engine"
"$HERE/components/strata.sh" "$CHROOT" || log "AVISO: no se pudo instalar Strata"
"$HERE/components/rea.sh" "$CHROOT" || log "AVISO: no se pudo instalar REA"
"$HERE/components/craft-anything.sh" "$CHROOT" || log "AVISO: no se pudo instalar Craft Anything"
"$HERE/components/hardened-malloc.sh" "$CHROOT" || log "AVISO: no se pudo compilar hardened_malloc"
"$HERE/components/llama-cpp.sh" "$CHROOT" || log "AVISO: no se pudo compilar llama.cpp (TISMA Code usará Strata)"
"$HERE/components/scx.sh" "$CHROOT" || log "AVISO: sin planificadores sched_ext (se usa el del kernel)"
"$HERE/components/escudo-ml.sh" "$CHROOT" || log "AVISO: sin motor ML de Escudo (sigue con firmas y YARA)"
"$HERE/components/python-extra.sh" "$CHROOT" || log "AVISO: sin ruff/sounddevice"
"$HERE/components/openjarvis.sh" "$CHROOT" || log "AVISO: no se pudo instalar OpenJarvis (opcional)"
fi
# Binario estático: funciona igual en el kernel genérico y en el kernel a medida
gcc -O2 -static -pthread -o "$LIB/centinela-membench" "$REPO/userspace/centinela/tools/membench.c"
gcc -O2 -static -pthread -o "$LIB/centinela-estres" "$REPO/userspace/centinela/tools/estres.c"
mkdir -p "$CHROOT/var/lib/centinela" "$CHROOT/var/lib/escudo"
chmod 700 "$CHROOT/var/lib/escudo"

# DNS del sistema: dnscrypt-proxy local (cifrado + bloqueo de rastreadores)
install -m 644 "$CHROOT/etc/resolv.conf.tisma" "$CHROOT/etc/resolv.conf.final"
rsync -a --exclude __pycache__ "$REPO/userspace/sandbox/tisma_sandbox" "$LIB/python/"
rsync -a --exclude __pycache__ "$REPO/userspace/copias/tisma_copias" "$LIB/python/"
in_chroot systemctl enable dnscrypt-proxy tisma-bloqueo.timer tisma-kernel-estado.timer auditd 2>/dev/null || true
in_chroot systemctl enable systemd-oomd tisma-copias-instantanea.timer tisma-copias-externo.timer 2>/dev/null || true
in_chroot systemctl enable chrony usbguard tisma-bloqueo-sesion tisma-wifi-banda tisma-scx 2>/dev/null || true
in_chroot systemctl enable rasdaemon tisma-salud.timer tisma-scrub.timer tisma-zram-recomprimir.timer 2>/dev/null || true
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
    in_chroot systemctl enable "$u" 2>/dev/null || log "AVISO: no se pudo habilitar $u"
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
# Lanzador del instalador en el escritorio live
mkdir -p "$CHROOT/etc/skel/Desktop"
[[ -f "$CHROOT/usr/share/applications/install-debian.desktop" ]] && \
    sed 's/^Name=.*/Name=Instalar TISMA OS/; s/^Icon=.*/Icon=tisma/' \
        "$CHROOT/usr/share/applications/install-debian.desktop" > "$CHROOT/etc/skel/Desktop/instalar-tisma.desktop"

in_chroot update-initramfs -u -k all
in_chroot apt-get clean
rm -rf "$CHROOT"/var/lib/apt/lists/* "$CHROOT"/tmp/* "$CHROOT"/root/.bash_history
# resolv.conf definitivo al final (durante la build el chroot usa el DNS del anfitrión)
mv -f "$CHROOT/etc/resolv.conf.final" "$CHROOT/etc/resolv.conf"
umount_chroot

# ---------------------------------------------------------------- 5. ISO
log "Creando squashfs (zstd)"
rm -rf "$ISODIR" && mkdir -p "$ISODIR/live" "$ISODIR/boot/grub"
mksquashfs "$CHROOT" "$ISODIR/live/filesystem.squashfs" -comp zstd -Xcompression-level 19 \
    -b 1M -noappend -e boot/efi -e .base-ok -e .packages-ok -quiet
KVER="$(find "$CHROOT/boot" -maxdepth 1 -name 'vmlinuz-*' -printf '%f\n' | sort -V | tail -n1 | sed 's/^vmlinuz-//')"
cp "$CHROOT/boot/vmlinuz-$KVER" "$ISODIR/live/vmlinuz"
cp "$CHROOT/boot/initrd.img-$KVER" "$ISODIR/live/initrd.img"
cp -r "$CHROOT/boot/grub/themes" "$ISODIR/boot/grub/"

LIVE="boot=live components quiet splash locales=es_ES.UTF-8 keyboard-layouts=es timezone=Europe/Madrid hostname=tisma username=tisma"
SEC="lsm=landlock,lockdown,yama,integrity,apparmor,bpf lockdown=integrity init_on_alloc=1 init_on_free=1 page_alloc.shuffle=1 randomize_kstack_offset=on slab_nomerge vsyscall=none iommu.strict=1"
cat > "$ISODIR/boot/grub/grub.cfg" <<EOF
insmod all_video
insmod gfxterm
insmod png
set gfxmode=auto
terminal_output gfxterm
set theme=/boot/grub/themes/centinela/theme.txt
set timeout=8

menuentry "Probar TISMA OS" {
    linux /live/vmlinuz $LIVE $SEC
    initrd /live/initrd.img
}
menuentry "Probar TISMA OS (gráficos básicos)" {
    linux /live/vmlinuz $LIVE $SEC nomodeset
    initrd /live/initrd.img
}
menuentry "Probar TISMA OS en RAM (más rápido, requiere 8 GB)" {
    linux /live/vmlinuz $LIVE $SEC toram
    initrd /live/initrd.img
}
EOF
log "Generando ISO híbrida BIOS+UEFI"
grub-mkrescue -o "$BUILD_DIR/$ISO_NAME" "$ISODIR" -- -volid TISMA_OS 2>&1 | tail -n 3
( cd "$BUILD_DIR" && sha256sum "$ISO_NAME" > "$ISO_NAME.sha256" )
log "Listo: $BUILD_DIR/$ISO_NAME ($(du -h "$BUILD_DIR/$ISO_NAME" | cut -f1))"
