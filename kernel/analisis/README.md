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
| Tiempo, VM sin KVM, muestra de prueba* | 15,2 s | 6,8 s (precalentada: 4,4–5,1 s) |

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

## VM precalentada (foto de la memoria)

Se arranca la VM **una vez** con `tisma.espera`: `init-analisis` deja todo listo, escribe `TISMA-LISTO` en la
consola y espera una orden. El anfitrión guarda entonces una foto de la memoria (QMP `stop` + `migrate
file:<foto>`, ~50 MB). Cada análisis arranca QEMU con la misma línea más `-incoming file:<foto>`, espera a que
cargue, `cont`, y manda por la consola `analizar <segundos> <semilla hex>`. Medido sin KVM
(`tests/analisis-vm-precalentada.py`): **restaurar tarda 0,06–0,14 s** en vez de ~4 s de arranque.

Seguridad:

- **Cada análisis parte de la misma foto limpia** y se descarta al terminar; nada pasa de un análisis a otro.
- **Generador aleatorio:** todos los análisis heredarían el mismo estado aleatorio de la foto. `init-analisis`
  mezcla la semilla nueva del anfitrión y fuerza la renovación (`RNDRESEEDCRNG`) antes de ejecutar nada; virtio-rng
  añade entropía continua. Comprobado: tres análisis seguidos leen bytes distintos de `/dev/urandom`.
  (VM Generation ID lo haría solo, pero la máquina microvm de QEMU no lo publica en ACPI.)
- **Falla cerrado:** orden desconocida, sin semilla o semilla no hexadecimal → la VM se apaga sin analizar.
- **La carpeta de la muestra (9p) no está en la foto:** se monta después de restaurar, con la muestra nueva.
- **Los sockets de control de QEMU (consola y QMP) van en una carpeta aparte**, fuera de la compartida.
- **La foto es del anfitrión:** root, permisos 0600, en `/var/lib/escudo`; se rehace cuando cambian el kernel o
  el squashfs (guardar su SHA-256 junto a la foto). Compatible con `-sandbox spawn=deny` (no usa `exec:`).

## Muestra por discos (sin carpeta compartida)

En lugar de compartir una carpeta por 9p (un servidor de archivos del anfitrión al alcance de la muestra, con
historial de fallos), la muestra entra y el resultado sale por dos discos virtuales reconocidos por su número de
serie:

- `tisma-entrada` (solo lectura): un tar con `orden.json` y `muestra/`, relleno hasta un tamaño fijo (64 MB).
- `tisma-salida` (16 MB): `TISMA-TRAZA <bytes>` y la traza. El anfitrión solo interpreta esa cabecera y lee como
  mucho ese número de bytes: no monta nada ni interpreta un sistema de archivos escrito por la VM.

El invitado lee y escribe estos discos **sin caché** (`iflag/oflag=direct`, `blockdev --flushbufs`): en la VM
precalentada el kernel pudo leerlos antes de la foto y su caché tendría el contenido de entonces. Comprobado: la
foto se toma con la entrada vacía y cada análisis lee la muestra nueva (`DISCOS=1
tests/analisis-vm-precalentada.py`, misma traza que por 9p). Funciona igual con QEMU y con Cloud Hypervisor.

## Cloud Hypervisor (en lugar de QEMU)

VMM en Rust: el mismo aislamiento por hardware (KVM) con ~50 000 líneas en vez de ~2 millones de QEMU (en C),
seccomp sobre sí mismo por defecto, **Landlock** (solo puede abrir el kernel, el squashfs y su carpeta) y foto /
restauración incluidas. No está en Debian: `kernel/analisis/compilar-ch.sh` lo compila en la versión fijada
(v54.1, commit fijado, `--locked`); binario de 6 MB.

**Necesita KVM**: no se puede probar en emulación. `tests/analisis-ch-test.py` (preparar / analizar / arrancar)
queda listo para un equipo con KVM; aquí solo se comprobó que acepta toda la configuración.
