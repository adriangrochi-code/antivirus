# Máquina virtual de análisis rápida (Escudo)

Escudo ejecuta cada programa desconocido, la primera vez, en una máquina virtual desechable
(`userspace/escudo/escudo/detonar.py`). Hasta ahora esa VM arranca la ISO mínima de TISMA completa: QEMU q35,
arranque «live», systemd y 1,5 GB de RAM, cada vez. Aquí está la versión rápida, **con el mismo aislamiento**:

| | Clásica | Rápida |
|---|---|---|
| Máquina QEMU | q35 (PCI, BIOS, muchos dispositivos emulados) | `microvm` (solo virtio por MMIO) |
| Kernel | el del sistema, con módulos | perfil `analisis`: diminuto, sin módulos ni BPF |
| Arranque | initramfs + live-boot + systemd | el squashfs es la raíz; `init-analisis` y apagado |
| RAM | 1536 MB | 512 MB |
| Tiempo, VM sin KVM, muestra de prueba* | 15,2 s | 6,8 s |

\* `tests/analisis-vm-test.sh`, 3 repeticiones, emulación TCG (sin KVM); la clásica ni siquiera incluye aquí el
arranque «live» de la ISO. Con KVM ambas son mucho más rápidas; la proporción es lo que importa. La traza de
la muestra es la misma en las dos (se detecta igual la lectura de la clave señuelo).

**Seguridad:** igual o mejor. El aislamiento sigue siendo de hardware (KVM) más la misma jaula bubblewrap
dentro; además el kernel de la VM no puede cargar módulos ni usar BPF, y QEMU emula muchos menos dispositivos
(menos código del anfitrión al alcance de un programa malicioso que intente escapar de la VM).

## Piezas

- `analisis.config`: fragmento del perfil (`KERNEL_PROFILE=analisis kernel/build-kernel.sh`), que se suma a
  `10-security` y `15-superficie`.
- `rootfs/usr/lib/tisma/init-analisis`: primer proceso de la VM (sustituye a systemd).

## Arranque propuesto para `detonar.py` (`vm_argv`)

```
qemu-system-x86_64 -enable-kvm -cpu host -M microvm -m 512 -smp 2 -display none -no-reboot \
  -nodefaults -no-user-config -serial stdio -nic none \
  -sandbox on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny \
  -kernel vmlinuz-analisis \
  -append "console=ttyS0 root=/dev/vda rootfstype=squashfs ro init=/usr/lib/tisma/init-analisis
           tisma.detonar=<s> lsm=landlock,lockdown,yama,apparmor lockdown=integrity panic=-1 quiet
           no_timer_check pci=off" \
  -drive id=raiz,file=analisis.squashfs,format=raw,if=none,readonly=on -device virtio-blk-device,drive=raiz \
  -fsdev local,id=m,path=<carpeta>,security_model=mapped-xattr -device virtio-9p-device,fsdev=m,mount_tag=muestra
```

`analisis.squashfs` es el `live/filesystem.squashfs` de la ISO mínima (ya trae Python, bubblewrap y strace).
Sin KVM (solo pruebas) hay que añadir `tsc_early_khz=2000000`: en emulación pura el kernel a veces no logra
medir el reloj de la CPU y se queda esperando; con KVM la frecuencia la da el hipervisor.
