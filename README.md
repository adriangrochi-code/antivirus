# TISMA OS

Sistema operativo basado en Linux centrado en **rendimiento, seguridad e ingeniería y ciencia**.
La idea central es **adaptar el sistema operativo al hardware, no al revés**: se mide cada equipo
y se genera un kernel y una configuración a su medida. La seguridad va integrada en el kernel.

| Pieza | Qué hace |
|---|---|
| `kernel/` | Linux 7.3 (la rama estable más reciente: la LTS acumulaba CVE sin llevar) con fragmentos de configuración: seguridad (KSPP, lockdown, Landlock, AppArmor, BPF‑LSM, firmas obligatorias), superficie de ataque mínima, memoria comprimida (zram/zswap zstd, MGLRU, DAMON), telemetría (PSI, perf, BTF, `sched_ext`), compatibilidad (NTSYNC para Proton, binder para Waydroid) y comprobador de CVEs |
| `userspace/centinela` | Mide el hardware (CPU, cachés, latencia entre núcleos, RAM, PCIe de GPUs, discos, límites de potencia RAPL/GPU), genera el plan a medida, compila el kernel para *ese* equipo y adapta el sistema en tiempo real (`adaptd`) |
| `userspace/escudo` | Antivirus (firmas + heurística + cuarentena cifrada) e IPS de red (fuerza bruta SSH/RDP, escaneos) con bloqueo en nftables |
| `userspace/tisma-code` | Agente de programación con IA **100 % local** (estilo Claude Code) con arnés para modelos locales, documentos locales y herramientas de Hope |
| `userspace/asistente` | TISMA Asistente: doble palmada → saludo con voz local → rutina de trabajo |
| `rootfs/`, `branding/`, `iso/` | Capa del sistema endurecida, tema **TISMA Aero** (KDE Plasma 6, paleta de TISMA, estilo Windows 7 modernizado) y construcción de la ISO |

## Kernel adaptado al hardware

1. `centinela probe` y `centinela bench` miden el equipo: microarquitectura y extensiones de la CPU,
   núcleos P/E, dominios de L3 (CCD), ancho de banda y latencia de la RAM (L1→RAM), latencia núcleo a núcleo,
   enlace PCIe real vs máximo de cada GPU (detecta tarjetas en ranuras x4), latencia 4K de cada disco,
   compresión zstd vs lz4 **en esa CPU**, límites de potencia (PL1/PL2, power cap de GPU, perfiles de plataforma)
   y vulnerabilidades de CPU.
2. `centinela plan --uso juegos|escritorio|servidor|portatil --perfil silencioso|equilibrado|rendimiento`
   explica cada decisión: `-march=native`, solo el soporte de su fabricante de CPU, `NR_CPUS` justo, topología
   para núcleos asimétricos, drivers del disco raíz **integrados** (arranque sin cargar módulos), preempción/HZ,
   planificador `sched_ext`, algoritmo y tamaño de zram, planificador de E/S por disco, límites de potencia.
3. `centinela build-kernel --install` compila ese kernel en el equipo (15–60 min). El genérico queda en GRUB
   como respaldo. `--firmar-dkms` permite firmar módulos externos (driver NVIDIA para CUDA/Strata).
4. `adaptd` mide la presión (PSI), predice la memoria disponible (suavizado de Holt) y activa el modo juego
   al detectar Proton/Wine/Waydroid. Solo escribe en una lista blanca de rutas de `/proc` y `/sys`.

Nunca se desactivan las mitigaciones de CPU (`mitigations=auto` siempre).

Memoria e hilos:
- **DAMON_RECLAIM** (≤16 GB o GPU integrada): la memoria sin uso desde hace 1-2 min pasa a zram en dosis
  pequeñas (64 MiB y 10 ms de CPU por segundo como máximo), antes de que la falta de RAM provoque tirones.
- **systemd-oomd**: si una app mantiene la presión de memoria alta, se cierra esa app y no se congela el equipo;
  Escudo y el control térmico se protegen.
- **Hilos para IA**: centinela detecta núcleos físicos, SMT y módulos CMT (AMD Excavator: cada pareja de núcleos
  comparte FPU y L2) y fija los hilos de llama.cpp (un hilo por unidad al generar, todos con el prompt);
  TISMA Code mide cada modelo («⚙ hilos») y se queda con lo que digan los datos.
- Copias, precarga y comprobaciones corren en `tisma-segundo-plano.slice` (CPU/E/S con peso 20).

## Copias de seguridad

`tisma-copias`: instantáneas btrfs **de solo lectura** de / y /home (diarias y antes de cada actualización de
apt), restauración de archivos sin perder la versión actual, `volver-atras` del sistema completo (al reiniciar)
y copia externa cifrada con restic (`tisma-copias externo configurar /media/…`), que se omite si el disco no
está conectado. El instalador usa btrfs con subvolúmenes `@`, `@home`, `@snapshots`, `@cache`, `@log`.

## Seguridad

- Kernel: lockdown `integrity`, módulos firmados obligatorios, `init_on_alloc/free`, pila aleatoria, sin
  `/dev/mem` ni `/proc/kcore`, IOMMU estricto contra ataques DMA, BPF sin privilegios desactivado, etc.
- `make cve-check`: cruza la base OSV de CVEs del kernel con **el código que realmente compila** nuestro
  kernel. Clasifica cada CVE en corregible (actualizar el parche LTS), mitigado por la configuración,
  mitigable (subsistema prescindible: se desactiva en `60-cve-mitigacion.config`) o pendiente.
  `kernel/latest-lts.sh` avisa cuando hay un parche LTS más nuevo.
- Userspace: sysctl endurecido, firewall nftables que bloquea todo lo entrante por defecto, módulos raros
  bloqueados, servicios con sandbox de systemd, actualizaciones de seguridad automáticas,
  `tisma-seguridad` (Lynis, rkhunter/chkrootkit, AIDE, ClamAV + Escudo, USBGuard).
- Medidas tomadas de GrapheneOS (V8 sin optimizadores, USB bloqueado con la pantalla bloqueada, MAC por
  conexión, hora NTS, hardened_malloc en demonios de red, reinicio automático y modo estricto opcionales):
  ver [docs/GRAPHENEOS.md](docs/GRAPHENEOS.md).

## Programas de Windows y Android

- **Windows**: Wine + Steam (Proton), con NTSYNC en el kernel. `centinela-apps windows` instala el perfil de
  juegos (Steam, Wine, 32 bits) y Bottles y Heroic.
- **Android**: Waydroid (contenedor LXC, sin emular CPU). `centinela-apps android` descarga e inicia la imagen.

## Aplicaciones por perfil (ISO liviana)

La ISO (< 2 GB, para descargarla desde una release de GitHub) trae la base: escritorio, multimedia, navegador,
seguridad y TISMA Code con llama.cpp. LibreOffice se descarga solo al instalar en el disco. Las
apps de cada uso se descargan al elegir el perfil en la Bienvenida o con `sudo tisma-perfil instalar <perfil>`,
desde los repositorios firmados de Debian: `diario`, `estudio`, `creacion`, `ingenieria`, `juegos`,
`programacion` (listas en `rootfs/usr/share/tisma/perfiles/`). Strata, REA y Craft Anything se descargan de
su release de GitHub con SHA-256 fijado: `sudo centinela-apps strata|rea|craft`. Para instalar sin conexión,
`TISMA_ISO_COMPLETA=1 make iso` lo mete todo en la ISO.

## TISMA Code

Interfaz tipo chat: eliges **carpeta/repo** y **modelo** y conversas.

- **Motor TISMA** (`tisma_code/motor.py`): la misma IA en cualquier equipo. Usa Strata (`127.0.0.1:8080`) si
  el equipo puede con él (GPU dedicada y ~32 GB entre RAM y VRAM); si no, llama.cpp con el modelo descargado
  más capaz que vaya a velocidad usable. Lee la cabecera GGUF (capas, expertos, caché KV) y planifica: todo en
  la GPU si cabe, expertos MoE en RAM y atención en la GPU (`--n-cpu-moe`), capas repartidas o CPU, y el
  mayor contexto que cabe. «⚙ optimizar» (o `tisma-code motor --optimizar modelo.gguf`) mide dispositivo,
  reparto de expertos, hilos y modelo borrador, y guarda la más rápida para ese equipo.
- **Contexto para modelos pequeños**: cuaderno de trabajo persistente, salidas largas fuera del contexto,
  reinicio limpio al 60 % con un resumen por tramos y exploración en un contexto aparte (`explorar`).
- **Ingeniería** (`ingenieria`): OpenSCAD, malla, gmsh, OpenFOAM, ngspice, KiCad, Verilog/Yosys, Octave,
  FreeCAD, Blender y pandoc con recetas fijas en la jaula, plantillas que funcionan y comprobación de los
  números frente a la estimación previa (`esperado`).

- Cada escritura y cada comando pide permiso, con su diff. Los comandos se ejecutan en bubblewrap: solo ven
  el proyecto y no tienen red.
- **Arnés para modelos locales**: repara llamadas a herramientas mal formadas, ediciones tolerantes a la
  sangría, detección de bucles, compactación del contexto y prefijo estable (caché KV).
- **Verificación automática** (Loop Controller de Hope): al terminar se ejecutan las pruebas del proyecto. Si
  fallan, el fallo vuelve como crítica y, si hay un modelo más potente, se escala a él (UCB1).
- **Documentos locales**: `~/TISMA/Documentos` (tu biblioteca: md, txt, pdf, código) y `<proyecto>/.tisma`
  (descubrimientos y mejoras que guarda el agente). Búsqueda instantánea con FTS5.
- **Hope**: 53 herramientas exactas de ingeniería y ciencia, crítico de cálculos, consejo de 4 agentes con
  verificador, base de conocimiento con lecciones. Hope es privado: `userspace/tisma-code/fetch-hope.sh`
  copia en la build solo la parte que no es la red neuronal; no se versiona aquí.
- Las trayectorias **verificadas** se guardan como episodios para afinar (LoRA) el modelo local.
- **REA** (Reverse Engineer Anything) y **Craft Anything** como servidores MCP en bubblewrap: entender
  cualquier programa, web o archivo con evidencia citable. Craft Anything añade memoria de investigación
  (brief, plan, hipótesis, hallazgos, traspaso) y el ciclo Estudiar → Construir → Verificar → Refinar con
  comparaciones medidas. Modos: limpio (por defecto), profundo y ejecución; cada cambio de modo, la red y cada
  ejecución del objetivo piden permiso. Versión y SHA-256 fijados en `iso/components/craft-anything.sh`.

## Construir

```sh
make kernel-minimal     # kernel para VMs (rápido) — o `make kernel` para hardware real
make qemu-test          # arranca el kernel y comprueba LSMs, lockdown, firmas, PSI, binder, sched_ext
make cve-check          # CVEs abiertos que afectan a este kernel + fragmento de mitigación
make test               # pruebas de centinela, escudo, TISMA Code y asistente
make iso                # ISO híbrida BIOS/UEFI con Secure Boot (root; HOPE_SRC=ruta para incluir Hope)
```

Requisitos de la máquina de build (Debian/Ubuntu): `build-essential bc flex bison libssl-dev libelf-dev
libdw-dev dwarves debhelper debootstrap squashfs-tools xorriso grub-pc-bin grub-efi-amd64-bin mtools
librsvg2-bin rsync cpio zstd qemu-system-x86 busybox-static git python3-pip`.

## Estado

**TISMA OS 0.9 Preview**: ver [docs/PREVIEW.md](docs/PREVIEW.md) (qué trae, cómo probarla y limitaciones).

Probado: compilación del kernel `minimal` y arranque en QEMU con todas las protecciones activas, la ISO
completa arrancando en QEMU con **Secure Boot** (shim → GRUB → kernel firmados), comprobador de CVEs,
detección de hardware y benchmarks, planes de ajuste, Escudo, TISMA Code (contra un motor simulado), las
herramientas de ingeniería con sus plantillas, la Bienvenida en un navegador real y ~225 pruebas automáticas
(`make test`, también en la CI de GitHub).

Pendiente de probar: el perfil `generic` del kernel (tarda horas en compilar), el kernel a medida en hardware
real, Waydroid/Proton/Strata en hardware real, la compatibilidad gráfica con GPU AMD GCN 1/2 y los límites de
potencia en equipos con RAPL.
