#!/usr/bin/env bash
# Arranca la ISO por el camino REAL de un PC con Secure Boot activado: firmware UEFI (OVMF) con las claves de
# Microsoft → shim → GRUB firmado → kernel firmado de Debian → sesión live. Comprueba en la consola del firmware
# que Secure Boot estaba activo y que no hubo firmas rechazadas, y guarda una captura de la pantalla al final.
# Uso: tests/qemu-secureboot-test.sh <iso> [segundos de espera, 600] [captura.png]
set -euo pipefail
ISO="${1:?ruta de la ISO}"; ESPERA="${2:-600}"; CAPTURA="${3:-secureboot-captura.png}"
CODE=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd; VARS_MS=/usr/share/OVMF/OVMF_VARS_4M.ms.fd
[[ -f $CODE && -f $VARS_MS ]] || { echo "Falta OVMF con Secure Boot (paquete ovmf)"; exit 2; }
W="$(mktemp -d)"
# Órdenes al monitor de QEMU por su socket (sin depender de socat)
mon() { python3 -c 'import socket,sys,time; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); time.sleep(0.3); s.recv(65536); s.sendall((sys.argv[2]+"\n").encode()); time.sleep(1)' "$W/mon" "$1" 2>/dev/null || true; }
trap 'mon quit; pkill -f "qemu-system-x86_64.*$W" 2>/dev/null || true; rm -rf "$W"' EXIT
cp "$VARS_MS" "$W/vars.fd"
KVM=(); [[ -w /dev/kvm ]] && KVM=(-enable-kvm -cpu host)
qemu-system-x86_64 "${KVM[@]}" -machine q35,smm=on -global driver=cfi.pflash01,property=secure,value=on \
    -m 4096 -smp 2 -no-reboot -display none -vga std -serial "file:$W/serie.log" \
    -monitor "unix:$W/mon,server,nowait" -name "tisma-sb-$W" \
    -drive if=pflash,format=raw,unit=0,readonly=on,file="$CODE" -drive if=pflash,format=raw,unit=1,file="$W/vars.fd" \
    -drive file="$ISO",media=cdrom,readonly=on -netdev user,id=n0 -device virtio-net-pci,netdev=n0 &
sleep "$ESPERA"
mon "screendump $W/pantalla.ppm"
sleep 2
python3 - "$W/pantalla.ppm" "$CAPTURA" <<'EOF' || true
import sys
from PIL import Image
Image.open(sys.argv[1]).save(sys.argv[2])
EOF
LOG="$(tr -d '\r' < "$W/serie.log" | sed 's/\x1b\[[0-9;]*[A-Za-z]//g')"
fallo=0
check() { if grep -qaE "$2" <<<"$LOG"; then echo "  ok   $1"; else echo "  FALLO $1"; fallo=1; fi; }
echo "Arranque con Secure Boot de $ISO:"
check "el kernel arranca con Secure Boot activo" "EFI stub: UEFI Secure Boot is enabled"
if grep -qaE "bad shim signature|Verification failed|Access Denied|Security Violation" <<<"$LOG"; then
    echo "  FALLO firma rechazada:"; grep -aE "bad shim signature|Verification failed|Access Denied|Security Violation" <<<"$LOG"
    fallo=1
else
    echo "  ok   ninguna firma rechazada"
fi
[[ -s "$CAPTURA" ]] && echo "  captura: $CAPTURA" || echo "  (sin captura de pantalla)"
exit $fallo
