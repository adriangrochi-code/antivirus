#!/usr/bin/env bash
# hardened_malloc de GrapheneOS (MIT, github.com/GrapheneOS/hardened_malloc), variante «light», para los
# demonios del sistema que hablan con la red o con datos no confiables (sshd, NetworkManager, polkit, cups,
# avahi, wpa_supplicant, bluetoothd, ModemManager, usbguard). NO es global: Chromium usa su propio asignador
# (PartitionAlloc), Flatpak lo ignora y Steam/Wine de 32 bits no pueden cargarlo.
# Se compila DENTRO del chroot (misma glibc que el sistema final) y sin -march=native.
# Uso: hardened-malloc.sh <chroot>
set -euo pipefail
CHROOT="$1"
# Commit FIJADO en el repositorio. Para actualizar: revisar el diff y cambiar el hash.
HM_COMMIT="${HM_COMMIT:-bf37d54560a62b50da72e8fda56abf04f23b5fd2}"
log() { printf '\033[1;35m[hardened_malloc]\033[0m %s\n' "$*"; }
SRC="$CHROOT/usr/src/hardened_malloc"
rm -rf "$SRC"
git clone -q https://github.com/GrapheneOS/hardened_malloc.git "$SRC"
git -C "$SRC" checkout -q "$HM_COMMIT"
[[ "$(git -C "$SRC" rev-parse HEAD)" == "$HM_COMMIT" ]] || { log "commit inesperado"; exit 1; }
chroot "$CHROOT" /usr/bin/env -i PATH=/usr/bin:/bin HOME=/root \
    make -C /usr/src/hardened_malloc VARIANT=light CONFIG_NATIVE=false CONFIG_WERROR=false -j"$(nproc)"
install -m 644 "$SRC/out-light/libhardened_malloc-light.so" "$CHROOT/usr/lib/libhardened_malloc-light.so"
install -D -m 644 "$SRC/LICENSE" "$CHROOT/usr/share/doc/hardened-malloc/LICENSE"
echo "$HM_COMMIT" > "$CHROOT/usr/share/doc/hardened-malloc/COMMIT"
rm -rf "$SRC"
log "Instalado /usr/lib/libhardened_malloc-light.so ($HM_COMMIT)"
