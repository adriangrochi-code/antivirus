#!/usr/bin/env bash
# Arranca una ISO de TISMA en QEMU con consola serie y comprueba que llega al inicio de sesión sin servicios
# fallidos, con lockdown y los LSM activos, y con los servicios básicos de TISMA en marcha.
# Uso: tests/qemu-iso-test.sh <iso>   (sin KVM tarda unos minutos)
set -euo pipefail
ISO="${1:?ruta de la ISO}"
W="$(mktemp -d)"
trap 'pkill -f "qemu-system-x86_64.*$W" 2>/dev/null || true; rm -rf "$W"' EXIT
xorriso -osirrox on -indev "$ISO" -extract /live/vmlinuz "$W/vmlinuz" -extract /live/initrd.img "$W/initrd.img" \
    >/dev/null 2>&1
APPEND="boot=live components console=ttyS0,115200 systemd.show_status=1 lsm=landlock,lockdown,yama,integrity,apparmor,bpf lockdown=integrity init_on_alloc=1 init_on_free=1 slab_nomerge iommu.strict=1 panic=10"
KVM=(); [[ -w /dev/kvm ]] && KVM=(-enable-kvm -cpu host)
qemu-system-x86_64 "${KVM[@]}" -m 2048 -smp 2 -nographic -no-reboot -kernel "$W/vmlinuz" -initrd "$W/initrd.img" \
    -append "$APPEND" -drive file="$ISO",media=cdrom,readonly=on -name "tisma-test-$W" \
    -netdev user,id=n0 -device virtio-net-pci,netdev=n0 > "$W/serie.log" 2>&1 &
for _ in $(seq 1 300); do
    grep -qaE "login:|Kernel panic" "$W/serie.log" && break
    sleep 5
done
LOG="$(sed 's/\x1b\[[0-9;]*m//g' "$W/serie.log")"
fallo=0
check() { if grep -qaE "$2" <<<"$LOG"; then echo "  ok   $1"; else echo "  FALLO $1"; fallo=1; fi; }
echo "Arranque de $ISO:"
check "llega al inicio de sesión" "login:"
check "lockdown activo" "Kernel is locked down"
check "LSM landlock/yama/apparmor/bpf" "LSM: initializing lsm=.*landlock.*yama.*apparmor.*bpf"
for u in nftables.service dnscrypt-proxy.service chrony.service usbguard.service systemd-oomd.service \
         auditd.service tisma-bloqueo-sesion.service dev-zram0.swap; do
    # systemd recorta los nombres largos en la consola («tisma-bloqueo-sesion.servi…»): basta el principio
    check "$u" "(Started|Finished|Activated swap) ${u:0:24}"
done
if grep -qa "\[FAILED\]" <<<"$LOG"; then echo "  FALLO hay servicios fallidos:"; grep -a "\[FAILED\]" <<<"$LOG"; fallo=1
else echo "  ok   ningún servicio fallido"; fi
exit $fallo
