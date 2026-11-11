#!/usr/bin/env bash
# Ensambla la ISO híbrida (BIOS + UEFI) arrancable CON Secure Boot activado.
#
# UEFI: shim firmado por Microsoft (paquete shim-signed de Debian) → GRUB firmado por Debian (gcdx64, la variante
# para CD/USB) → kernel firmado por Debian. Junto a shim va MokManager (mmx64), para registrar la clave propia de
# TISMA (MOK) con la que se firma el kernel TISMA del sistema instalado. BIOS: GRUB normal (sin firma: el BIOS
# clásico no la comprueba). grub-mkrescue no servía: genera un GRUB sin firmar que Secure Boot rechaza.
#
# Uso: secureboot-iso.sh <isodir> <raíz con usr/lib/shim y usr/lib/grub/x86_64-efi-signed> <salida.iso> [volid]
# El GRUB firmado solo usa módulos incluidos en su imagen: grub.cfg no debe cargar módulos de disco en UEFI.
set -euo pipefail
ISODIR="$1"; SIGNED="$2"; OUT="$3"; VOLID="${4:-TISMA_OS}"
log() { printf '\033[1;35m[secureboot]\033[0m %s\n' "$*"; }

SHIM="$SIGNED/usr/lib/shim/shimx64.efi.signed"
MM="$SIGNED/usr/lib/shim/mmx64.efi.signed"
GRUBCD="$SIGNED/usr/lib/grub/x86_64-efi-signed/gcdx64.efi.signed"
for f in "$SHIM" "$MM" "$GRUBCD"; do [[ -f "$f" ]] || { echo "Falta $f (paquetes shim-signed, shim-helpers-amd64-signed, grub-efi-amd64-signed)" >&2; exit 1; }; done

# El GRUB firmado de Debian busca la raíz del medio por el archivo .disk/info y su configuración en /boot/grub
mkdir -p "$ISODIR/.disk" "$ISODIR/EFI/BOOT" "$ISODIR/EFI/debian" "$ISODIR/boot/grub/i386-pc"
echo "TISMA OS $(date +%Y%m%d)" > "$ISODIR/.disk/info"
STUB="$(mktemp)"
cat > "$STUB" <<'EOF'
search --no-floppy --set=root --file /.disk/info
set prefix=($root)/boot/grub
configfile $prefix/grub.cfg
EOF
install -m 644 "$SHIM" "$ISODIR/EFI/BOOT/BOOTX64.EFI"
install -m 644 "$GRUBCD" "$ISODIR/EFI/BOOT/grubx64.efi"
install -m 644 "$MM" "$ISODIR/EFI/BOOT/mmx64.efi"
install -m 644 "$STUB" "$ISODIR/EFI/debian/grub.cfg"

# Partición EFI (FAT) que el firmware lee al arrancar desde USB o DVD
EFIIMG="$(mktemp --suffix=.img)"
trap 'rm -f "$STUB" "$EFIIMG"' EXIT
truncate -s 16M "$EFIIMG"
mkfs.vfat -n TISMA_EFI "$EFIIMG" >/dev/null
mmd -i "$EFIIMG" ::/EFI ::/EFI/BOOT ::/EFI/debian
mcopy -i "$EFIIMG" "$ISODIR/EFI/BOOT/BOOTX64.EFI" "$ISODIR/EFI/BOOT/grubx64.efi" "$ISODIR/EFI/BOOT/mmx64.efi" ::/EFI/BOOT/
mcopy -i "$EFIIMG" "$STUB" ::/EFI/debian/grub.cfg
mcopy -i "$EFIIMG" "$STUB" ::/EFI/BOOT/grub.cfg

# BIOS: imagen El Torito de GRUB (sin firma) y sus módulos
cp /usr/lib/grub/i386-pc/*.mod /usr/lib/grub/i386-pc/*.lst "$ISODIR/boot/grub/i386-pc/" 2>/dev/null || true
grub-mkimage -O i386-pc-eltorito -p /boot/grub -o "$ISODIR/boot/grub/i386-pc/eltorito.img" \
    biosdisk iso9660 part_msdos part_gpt normal configfile search search_fs_file linux echo test all_video \
    gfxterm png font

log "Generando $OUT (BIOS + UEFI con Secure Boot)"
xorriso -as mkisofs -iso-level 3 -full-iso9660-filenames -joliet -joliet-long -rational-rock \
    -volid "$VOLID" \
    -eltorito-boot boot/grub/i386-pc/eltorito.img -no-emul-boot -boot-load-size 4 -boot-info-table \
    --grub2-boot-info --grub2-mbr /usr/lib/grub/i386-pc/boot_hybrid.img \
    -eltorito-catalog boot/grub/boot.cat \
    -eltorito-alt-boot -e --interval:appended_partition_2:all:: -no-emul-boot \
    -append_partition 2 0xef "$EFIIMG" -appended_part_as_gpt \
    -o "$OUT" "$ISODIR" 2>&1 | grep -vE "^xorriso : (UPDATE|NOTE)" | tail -n 5
