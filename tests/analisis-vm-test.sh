#!/usr/bin/env bash
# Compara la máquina virtual de análisis de Escudo en sus dos formas, con la misma muestra (lee una clave SSH
# señuelo) y el mismo sistema de análisis (squashfs):
#   clasico: q35 + kernel normal + systemd (como hoy, pero sin el arranque «live» de la ISO, que tarda más)
#   rapido:  microvm + kernel «analisis» + init-analisis (kernel/analisis)
# Uso: tests/analisis-vm-test.sh clasico|rapido <bzImage> <rootfs.sqfs> [segundos]
set -euo pipefail
modo=$1 kernel=$2 sqfs=$3 seg=${4:-10}
w=$(mktemp -d)
mkdir -p "$w/muestra"
printf '#!/bin/sh\ncat "$HOME/.ssh/id_ed25519" > /dev/null\necho hola > /tmp/x\n' > "$w/muestra/muestra.sh"
chmod 755 "$w/muestra/muestra.sh"
echo '{"muestra": "muestra.sh", "args": []}' > "$w/orden.json"
seg_lsm="lsm=landlock,lockdown,yama,apparmor,bpf lockdown=integrity"
ACCEL=(); TSC="tsc_early_khz=2000000"   # sin KVM no hay reloj de referencia: frecuencia fija solo en pruebas
[[ -w /dev/kvm ]] && ACCEL=(-enable-kvm -cpu host) && TSC=""
t0=$(date +%s.%N)
if [[ $modo == clasico ]]; then
    timeout 900 qemu-system-x86_64 "${ACCEL[@]}" -m 1536 -smp 2 -nographic -no-reboot -nic none \
        -kernel "$kernel" -append "console=ttyS0 root=/dev/vda rootfstype=squashfs ro tisma.detonar=$seg $seg_lsm panic=5" \
        -drive file="$sqfs",format=raw,if=virtio,readonly=on \
        -virtfs local,path="$w",mount_tag=muestra,security_model=mapped-xattr,id=muestra > "$w/consola.txt" 2>&1 || true
else
    timeout 900 qemu-system-x86_64 "${ACCEL[@]}" -M microvm -m 512 -smp 2 -display none -no-reboot -nodefaults \
        -no-user-config -serial stdio \
        -kernel "$kernel" -append "console=ttyS0 root=/dev/vda rootfstype=squashfs ro init=/usr/lib/tisma/init-analisis tisma.detonar=$seg $seg_lsm panic=-1 quiet no_timer_check pci=off $TSC" \
        -drive id=raiz,file="$sqfs",format=raw,if=none,readonly=on -device virtio-blk-device,drive=raiz \
        -fsdev local,id=m,path="$w",security_model=mapped-xattr -device virtio-9p-device,fsdev=m,mount_tag=muestra \
        > "$w/consola.txt" 2>&1 || true
fi
t1=$(date +%s.%N)
lineas=$(wc -l < "$w/traza.txt" 2>/dev/null || echo 0)
senuelo=$(grep -c "id_ed25519" "$w/traza.txt" 2>/dev/null || echo 0)
printf 'modo=%s total_s=%.1f traza_lineas=%s lee_señuelo=%s\n' "$modo" "$(echo "$t1 - $t0" | bc)" "$lineas" "$senuelo"
[[ "${VER:-0}" == 1 ]] && tail -n 25 "$w/consola.txt"
[[ -n "${GUARDAR:-}" ]] && cp "$w/traza.txt" "$GUARDAR" 2>/dev/null
rm -rf "$w"
