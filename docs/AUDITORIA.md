# Auditoría de seguridad y errores — noviembre de 2026

Revisión completa de TISMA OS: análisis automático de todo el repositorio y revisión manual, por áreas, de
Escudo, TISMA Code, los componentes que corren como root (Centinela, autoajuste, copias, sandbox, asistente) y
la imagen del sistema (construcción, unidades, polkit, firewall, cadena de suministro). Cada hallazgo se
verificó contra el código real (lectura y, cuando se pudo, prueba de concepto) antes de corregirlo; los
corregidos llevan prueba de regresión cuando es posible.

**Estado final:** 136 pruebas pasan (TISMA Code 54, Escudo 31, Centinela 17, sandbox 16, copias 9, asistente
6, Rendimiento 3); ruff, shellcheck, `nft -c` y `systemd-analyze verify` sin errores.

**Límite importante:** nada de esto se ha probado aún en una ISO construida ni en la ThinkPad A275. Varias
correcciones de la imagen (initramfs, DNS, instalador) solo se confirmarán al construir y arrancar la ISO.

## Críticos (4, corregidos)

| Área | Fallo | Corrección |
|---|---|---|
| TISMA Code | Un repositorio con `.tisma/rea` (o `craft`) como enlace a `~` hacía que la jaula de REA/Craft **montara la carpeta personal con escritura** (confirmado: lectura y escritura reales) | `safe_dir`: cada componente se crea y comprueba; enlaces o no-carpetas se rechazan |
| Imagen | `MODULES=dep` rompía `update-initramfs` en el chroot: **la ISO no se construía** | `most` en la imagen; centinela pone `dep` al instalar |
| Imagen | dnscrypt-proxy no arrancaba (puerto ocupado por su socket de Debian, lista de bloqueo inexistente): **sistema sin DNS** | Activación por socket, lista creada por tmpfiles, caché absoluta, AF_UNIX para tisma-bloqueo |
| Imagen | 9 paquetes de la lista no existían en trixie y se omitían en silencio; entre ellos **llama.cpp** (motor de TISMA Code) y **lightgbm** (ML de Escudo); «piper» era un configurador de ratones | llama.cpp compilado (versión fijada, CPU multivariante + Vulkan); ruedas con SHA-256 fijado; `make check-paquetes` |

## Altos (13, corregidos)

| Área | Fallo | Corrección |
|---|---|---|
| TISMA Code | La herramienta `git` (sin aprobación) permitía **ejecutar comandos** con `git grep -O`/`--open-files-in-pager`, y las abreviaturas (`--outp`) esquivaban la lista negra | Lista blanca de opciones |
| Escudo | Tras reiniciar, las IPs bloqueadas **dejaban de estarlo** y nunca se volvían a bloquear | Se reinstalan al arrancar con el tiempo restante; caducadas re-bloqueables; reinicio si se recarga nftables |
| Centinela | La autorización de overclock (pkexec, recordada) valía para **compilar e instalar un kernel desde una carpeta del usuario** → root | Con pkexec solo `oc`, `autotune`, `estado`, `arranque` |
| Centinela | `firstboot`/`power` no podían escribir `/etc/initramfs-tools` ni `/etc/xdg` y el error cortaba todo (bench en cada arranque); `autotune-arranque` fallaba por `/run/centinela` | Rutas permitidas, error por archivo, `RuntimeDirectory` |
| Centinela | `apply` (en cada arranque) **borraba el perfil del autoajuste** y el overclock elegido | Las preferencias se combinan, no se sobrescriben |
| Copias | Restaurar en una carpeta de usuario: carrera que permitía que root **sobrescribiera cualquier archivo** | La escritura se hace con la identidad del dueño (setpriv); probado con `nobody` |
| Imagen | Sin `VERSION_CODENAME`, **unattended-upgrades no aplicaba ninguna actualización de seguridad** | os-release propio con codename, desviado con dpkg-divert |
| Imagen | USBGuard exige 0600 y git no guarda permisos: no arrancaba (bloqueo de USB inactivo) | Permisos fijados en la build |
| Imagen | LUKS2 + `/boot` cifrado: **GRUB no arranca** el sistema instalado | LUKS1 hasta tener /boot aparte o UKI |
| Sandbox | `/dev/uinput` en el perfil de Wine: un .exe podía fabricar un teclado virtual y escribir en una terminal | uinput solo para Steam (Steam Input), como permiso aparte |
| Imagen | tisma_engine no se instalaba nunca («vlatest») y, si fallaba el hash, compilaba una etiqueta mutable como root fuera del chroot | Release fijada o error; `main` solo a petición y en el chroot |
| Imagen | debootstrap podía continuar **sin verificar firmas** | `--keyring` con las claves fijadas y `--force-check-gpg`; https |
| Autoajuste | `centinela-estres` daba **falso aprobado** si no había memoria para la prueba | Cuenta como fallo |

## Medios (corregidos)

- **Escudo:** congelado por ransomware ahora cuenta por usuario, usa pidfd y verifica ejecutable y dueño antes
  del SIGSTOP, nunca congela procesos de root y no sigue enlaces ni se bloquea en FIFOs; el «escaneo de
  puertos» (SYN falsificable) ya no bloquea el router/NAS/VPN de la red local; se protegen la puerta de
  enlace IPv6 y los servidores de tisma-vpn; `audit-usuarios` seguía enlaces intermedios.
- **Firewall:** `flush ruleset` en nftables.conf **borraba el kill-switch de la VPN** al recargar.
- **TISMA Code:** `docs_guardar` escribía fuera del proyecto por un enlace en `.tisma/<tipo>`; al cambiar de
  proyecto quedaban vivos llama-server y los servidores MCP; especialistas solo con endpoint local.
- **Autoajuste:** marca «en prueba» antes de aplicar cada paso; el undervolt se acumulaba (ahora parte de la
  curva de fábrica); reversión a fábrica ante cualquier error; prueba fallida si muere la carga de GPU; la
  reversión por temperatura y el overclock del usuario ya no se pisan; adaptd sigue los cambios de perfil.
- **Kernel a medida:** con raíz cifrada no integraba el driver del disco ni dm-crypt; `build-kernel` usa la
  última versión LTS y borra la clave de firma si no se pide `--firmar-dkms`; `panic=10` se perdía.
- **Sandbox:** se aceptaban `~/.config` o `~/.local` enteros; los enlaces no se resolvían al montar.
- **Copias:** restic no detectaba el mismo disco entre subvolúmenes y se copiaba a sí mismo desde /home.
- **Imagen:** clave de Waydroid (signed-by aceptaba claves añadidas); REA no se instalaba (npm sin node en
  el PATH); OpenJarvis con herramientas sin fijar y fallo en el primer arranque; `/etc/centinela` inexistente;
  Makefile con `check-latest` roto.

## Bajos (corregidos)

Rotación de logs recursiva y lector de journald sin reintento (Escudo); endpoint de «explicar» validado por
prefijo; tabla nft preexistente; `/api/docs/read` sin sesión; tamaño de RAM SMBIOS ×1024; cambios anotados
aunque fallara la escritura; límite de potencia «silencioso» acumulativo; rutas relativas con pkexec;
rutina del asistente sobrescrita con errores, saludo con llaves inválidas, texto de voz que empieza por «-»;
GAPPS perdido con sudo; versiones `-rc` en check-cves; reservas sin comprobar en membench/estres; carácter
RTLO literal en el código; scrub con límite de 2 h; mDNS, KDE Connect y MLD bloqueados; reglas de auditd solo
de 64 bits; opciones de Kconfig que ya no existen.

## Riesgos residuales (no corregidos, con motivo)

- **Sandbox con red:** comparte el espacio de red del sistema y puede alcanzar el socket X11 abstracto aunque
  el perfil no conceda X11. Solución prevista: red propia con `pasta`/slirp4netns.
- **REA:** sus dependencias transitivas de npm se resuelven en la build (el paquete no publica lockfile);
  mitigado con integridad del paquete principal y `--ignore-scripts`.
- **Tokens en la URL** de TISMA Code y Rendimiento (visibles en `/proc` sin `hidepid` y en el historial;
  se borran de la barra con `history.replaceState`).
- **`_check_local_owner`** cachea 30 s la comprobación del dueño del puerto del motor.
- **Reloj muy desfasado** en el primer arranque: DoH y NTS dependen de certificados con fecha.
- `kernel.kexec_load_disabled` genera un aviso en el registro con KEXEC desactivado; `integrity` en `lsm=`
  se ignora (IMA/EVM se activan igualmente).
- **Pendientes de diseño:** Secure Boot/UKI con LUKS2 + TPM2, restricción de user namespaces por AppArmor.
