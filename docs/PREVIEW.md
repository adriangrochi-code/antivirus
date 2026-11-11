# TISMA OS 0.9 Preview

Versión de prueba para probar el sistema completo y reunir fallos antes de la 1.0. **No es para uso diario
ni para equipos con datos importantes sin copia.**

## Qué trae

- **Escritorio**: KDE Plasma 6 con el aspecto TISMA (claro u oscuro, color de acento, iconos Papirus, Inter),
  pantalla de arranque con logo, instalador con la marca TISMA y la **Bienvenida** del primer inicio (aspecto,
  apps a mano según tu uso, IA local y estado de la protección).
- **Seguridad**: Escudo (antivirus con firmas, YARA y comportamiento; IPS de red; puerta de primera ejecución
  que analiza en una máquina virtual lo descargado antes de dejarlo ejecutar), cortafuegos nftables, DNS
  cifrado sin rastreadores, USB desconocidos bloqueados con la sesión bloqueada, AppArmor, kernel endurecido
  con lockdown y módulos firmados, cifrado del disco en el instalador, copias e instantáneas btrfs.
- **Rendimiento**: Centinela adapta el sistema al equipo (CPU, memoria, disco, gráfica, batería): zram con
  recompresión, compresión anticipada de la memoria de las apps en segundo plano, planificador por disco,
  ahorro de energía sin perder funciones, compatibilidad gráfica (GPU AMD GCN 1/2 con Vulkan, WineD3D/Lavapipe
  en GPU sin Vulkan), TRIM a través del cifrado y aviso de desgaste del SSD.
- **IA local (TISMA Code)**: agente de programación, cálculo, CAD y simulación con el motor que mejor rinde en
  tu equipo (Strata o llama.cpp), herramientas por kits, cuaderno de trabajo y reinicio limpio del contexto
  para modelos pequeños, herramientas de ingeniería con plantillas y comprobación de resultados.
- **Aplicaciones**: ofimática, correo, multimedia, edición de vídeo y audio, grabación, diseño (GIMP, Inkscape,
  Krita, Blender), ingeniería (FreeCAD, KiCad, OpenSCAD, gmsh, ngspice, Octave…), Steam y Wine, Tienda con
  Flathub, y Mesa y firmware recientes (backports de Debian).

## Cómo probarla

1. **En una máquina virtual** (lo más seguro): QEMU/virt-manager o VirtualBox con UEFI, 4 GB de RAM o más y
   un disco de 40 GB.
2. **En un equipo real**: graba la ISO en un USB (`dd` o Ventoy) y arranca en modo live; desde el escritorio se
   puede instalar con «Instalar TISMA OS». **Secure Boot puede quedarse activado**.

## Secure Boot

La cadena es la estándar de Debian: shim (firmado por Microsoft) → GRUB (firmado por Debian) → kernel firmado.

- La sesión live, el instalador y, de inicio, el sistema instalado arrancan con el **kernel firmado de Debian**
  (backports, el más nuevo con firma): funciona con Secure Boot en cualquier PC y trae todos los drivers.
- El **kernel TISMA** (endurecido y ajustado a tu equipo) se firma con la clave propia de TISMA. Para usarlo con
  Secure Boot, regístrala una vez: `sudo tisma-secureboot activar`, reinicia y en la pantalla azul de MokManager
  elige «Enroll MOK» y escribe la contraseña de un solo uso que te mostró. `tisma-secureboot elegir` (al
  arrancar) solo pone el kernel TISMA por defecto cuando es seguro; si no, el de Debian: el equipo siempre
  arranca.
- Con Secure Boot desactivado, el kernel TISMA arranca directamente.
- Quien construye la ISO crea la clave con `iso/crear-clave-secureboot.sh` en su equipo y la pasa con
  `TISMA_SB_KEY`/`TISMA_SB_CERT`; la clave privada nunca va al repositorio. Sin clave, el kernel TISMA queda sin
  firmar (solo arranca con Secure Boot desactivado).
- Pendiente: kernel en imagen unificada (UKI) que firme también el initramfs y la línea de arranque, y
  desbloqueo del disco con TPM2 + PIN (requiere LUKS2).

## Limitaciones conocidas de la preview

- **Sin canal de actualizaciones propio**: las apps de TISMA (Escudo, Centinela, TISMA Code, Bienvenida…) no
  se actualizan solas en un sistema instalado; Debian (incluidas las de seguridad) sí. Llegará en una versión
  posterior con paquetes .deb y un repositorio firmado.
- **Solo en español**.
- **Kernel**: la ISO de prueba en máquina virtual usa el kernel `minimal`; para hardware real se construye con
  `make kernel` (perfil `generic`, todos los drivers).
- **Componentes compilados**: una ISO construida con `TISMA_SIN_COMPILAR=1` no trae llama.cpp (TISMA Code
  solo podrá usar Strata), hardened_malloc, planificadores sched_ext ni el motor ML de Escudo.
- Sin probar todavía en hardware real: la compatibilidad gráfica (cambio a amdgpu en GCN 1/2), la compresión
  de memoria de apps (cgroups reales), Waydroid, Proton y los límites de potencia.

## Informar de fallos

Abre una incidencia en https://github.com/adriangrochi-code/antivirus/issues con: modelo del equipo, qué
hacías, qué esperabas y qué pasó, y la salida de `journalctl -b -p warning` (revísala antes de pegarla: puede
contener nombres de archivos o redes).
