# TISMA OS 0.9 Preview

Versión de prueba para probar el sistema completo y reunir fallos antes de la 1.0. **No es para uso diario
ni para equipos con datos importantes sin copia.**

## Novedades de la preview 12

- **Batería**: modos Eco · Equilibrado · Turbo. Eco rinde más por vatio; Turbo a batería ya no pierde velocidad
  por los ahorros del disco y la gráfica.
- **Aviso de calor**: si el procesador se frena seguido por temperatura (ventilador sucio), te avisa en simple.
- **TISMA Code**: «Sin apuro» (tareas largas con modelos más grandes que la memoria, trabaja enchufado y avisa
  al terminar), kit «construir» (360 guías para crear lo que sea desde cero), botón «Pensar», «✂ Reducir»
  modelos y primera respuesta casi inmediata.
- **Hope incluido** y TISMA bajo licencia **GPL-3.0**.
- Apps de TISMA que abren antes y navegador más liviano en equipos con poca memoria.

## Qué trae

- **Escritorio**: KDE Plasma 6 con el aspecto TISMA (claro u oscuro, color de acento, iconos Papirus, Inter),
  pantalla de arranque con logo, instalador con la marca TISMA y la **Bienvenida** del primer inicio (aspecto,
  apps a mano según tu uso, IA local y estado de la protección).
- **Kernel al día en seguridad**: el kernel TISMA sigue la rama ESTABLE más reciente (7.3), no la LTS: en
  noviembre de 2026 la LTS 6.18 tenía 153 CVE conocidas corregidas solo en ramas nuevas; la 7.3.4, ninguna. Sin
  protocolos raros, clasificadores de tráfico de routers ni sistemas de archivos de clúster (de ahí salen muchas
  escaladas de privilegios, como las 948 que encontró OpenAI); bloqueados también como módulos en el kernel de
  Debian. La ISO lleva todas las correcciones de seguridad de Debian publicadas al construirla.
- **Novedades de la preview 11**: arreglo importante del actualizador: en un equipo real quita el kernel TISMA
  «minimal» (solo para máquinas virtuales) como hace el instalador, y si el arranque quedara con él, deshace la
  actualización (en la A275, tras actualizar con la preview 10, se reiniciaba en bucle). **Zona segura**: clic
  derecho > «Abrir en zona segura» abre un archivo dudoso sin internet ni acceso a tus archivos y lo borra al
  cerrar. **Programas revisados por Escudo** en TISMA Seguridad: qué hizo cada programa, en simple.
- **Novedades de la preview 10 (uso diario más limpio)**: con TISMA instalado, el USB muestra el menú con
  «Actualizar o reinstalar TISMA» arriba y 10 s de cuenta atrás (en la preview 9 el aviso era una línea chica y en
  la A275 pasó desapercibido). Menú de inicio con pocas categorías y lo técnico en **Opciones avanzadas**; nombres
  que dicen qué hace cada programa (Fotos, Vídeos, Música, Lupa…); bandeja con red, volumen y batería y el resto
  detrás de la flecha; los enlaces se abren en TISMA Navegador (aislado); pantalla de carga con el logo al entrar;
  TISMA Code se ajusta solo para ir más rápido; lección «Actualizar TISMA» en la Guía.
- **Novedades de la preview 9 (actualizar sin perder nada)**: si el equipo ya tiene TISMA, al arrancar el USB
  se ve el menú con «Actualizar o reinstalar TISMA» arriba y 10 s de cuenta atrás (antes arrancaba el disco sin
  avisar). En una ThinkPad, para arrancar desde el USB con un sistema ya instalado, pulsa F12 al encender. Desde el USB, la ventana **Actualizar TISMA** actualiza el sistema conservando archivos, cuentas,
  wifi, ajustes y programas (los que la versión nueva no trae se reinstalan solos con internet); guarda la versión
  anterior para **volver a ella** con un botón, y también ofrece **Formatear e instalar de cero**. Si algo falla
  a mitad, el equipo queda como estaba. Probado de punta a punta en una máquina virtual con disco cifrado.
  El kernel TISMA se actualiza solo (con el equipo enchufado y libre) y solo avisa «reinicia cuando quieras».
  Avisos de Escudo en lenguaje simple. TISMA Code: ya no se queda en bucle con modelos pequeños ni muestra el
  texto técnico de sus herramientas, y tiene **Ajustes** con las descargas y su progreso. Arranque sin recuadro
  de consola ni cursor parpadeando.
- **Novedades de la preview 8 (más simple)**: tema TISMA Cristal (claro y oscuro) con fondos nuevos; **Guía TISMA**
  con 6 temas grandes al inicio y el resto en «Ver todos los temas»; **TISMA Seguridad** con botones (sin
  terminal); barra de tareas con pocos iconos (Archivos, Navegador, Tienda); menú con favoritos de nombre claro;
  el explorador se llama «Archivos» y ya no muestra las particiones internas del sistema; sin carpeta
  «Plantillas»; usos «Primaria» y «Liceo» en el instalador; aviso con botón «Era mío» si un USB se hace pasar por
  teclado; programas .exe revisados por Escudo antes de abrirse.
- **Novedades de la preview 7 (lo encontrado en la ThinkPad A275)**: el sistema instalado ya no arranca con el
  kernel de rescate que Secure Boot rechaza («firma shim incorrecta»): se quita en equipos reales y el menú se
  rehace cada vez que cambia un kernel. El icono de Wi-Fi, volumen y batería vuelve a la barra desde el primer
  inicio (la bandeja salía vacía). El panel táctil funciona con toques (tocar = clic) y se ignora mientras
  escribes. TISMA Code y TISMA Rendimiento se abren como aplicaciones con ventana propia, no como pestañas del
  navegador. En el menú de arranque la selección ya no tapa el logo y los títulos se leen bien con Secure Boot.
  **Varios usos a la vez**: en el instalador, la página «Tu uso» tiene casillas (diario, estudio, ingeniería,
  programación, juegos y creación) y «Elegir todo»; al terminar se descargan sumadas todas sus aplicaciones
  (sin Internet, en el primer arranque con conexión). Lo mismo en la Bienvenida. Lo elegido queda en
  `/etc/tisma/perfiles-elegidos`; si la descarga se cortó, `sudo tisma-perfil completar` la retoma.
  **Más fácil**: Dolphin ve los equipos y carpetas compartidas de la red local (SMB); clic derecho >
  Propiedades > Compartir comparte una carpeta con la red local (el puerto solo se abre a la red de casa y
  mientras haya algo compartido); miniaturas de fotos, vídeos y PDF; teléfonos por USB; teclas de brillo,
  suspender por inactividad y aviso de batería baja (en la A275 faltaban). En el USB, el instalador se abre al
  frente con «Siguiente» a la vista.
- **Novedades de la preview 6 (instalar y usar sin complicaciones)**: el USB arranca directo al instalador, sin menú (Esc
  durante 3 s muestra las opciones; si el equipo ya tiene TISMA instalado, arranca ese sistema). El instalador se
  abre solo y pregunta tres cosas: dónde instalar, la contraseña del disco y tu cuenta; la hora y los formatos
  salen de tu ubicación y el teclado es el del USB. Al terminar se reinicia solo. La contraseña del disco la pide
  la pantalla de arranque gráfica con el teclado en español (antes, GRUB en inglés). Arreglos de la preview 5:
  la sesión del USB no tenía usuario (pantalla negra con cursor), la instalación fallaba en equipos UEFI (faltaban
  dosfstools y squashfs-tools), un sistema cifrado no arrancaba (faltaba cryptsetup-initramfs), OpenSnitch
  bloqueaba el DNS y la hora, el DNS no volvía si se arrancaba sin red y no había icono para instalar. Si el
  arranque no llega a la pantalla de inicio, a los 4 minutos se muestra en pantalla qué falló.
  Probada instalando de verdad en una máquina virtual UEFI con Secure Boot, lo que destapó y corrigió más fallos:
  la protección de Escudo bloqueaba al instalador, faltaban herramientas para crear la partición EFI y copiar el
  sistema, el paso final de paquetes fallaba, el primer arranque elegía un kernel que Secure Boot rechaza, la
  pantalla de inicio de sesión no arrancaba (ahora va sobre Wayland), la sesión del USB bloqueaba la pantalla a
  mitad de instalar y el navegador pedía «crear una cartera». Además: iconos propios para cada app de TISMA y menú
  de arranque rediseñado.
  **Uso familiar** (probado en la máquina virtual): atajos de siempre con la tecla del logo (Logo+E archivos,
  Logo+R ejecutar, Logo+I preferencias, Logo+L bloquear, Logo+D escritorio, Logo+Shift+S recortar, Logo+V
  portapapeles, Logo+↑/↓ maximizar o minimizar, Ctrl+Shift+Esc monitor del sistema); clic derecho > Crear nuevo >
  documento de texto, hoja de cálculo o presentación; al entrar no se reabren las ventanas de la sesión anterior; la carpeta del
  escritorio se llama «Escritorio» (antes quedaba «Desktop» en inglés). La contraseña del disco se pide en
  español, el instalador ya no dice «Calamares» y la barra de tareas no muestra iconos rotos.
- **Novedades de la preview 5**: Escudo protege antes de que aparezca la pantalla de inicio de sesión; la puerta
  de primera ejecución y el análisis en aislamiento funcionan dentro del servicio (antes se apagaban o daban
  «limpio»); YARA y los modelos de IA se descargan solos; detecta malware sin archivo, carga de eBPF y puertas
  traseras por SSH; DNS que bloquea malware con una lista de amenazas diaria; disco cifrado por defecto en el
  instalador; kernel 7.3 y endurecimiento al nivel de Kicksecure.
- **Seguridad**: Escudo (antivirus con firmas, YARA y comportamiento; IPS de red; puerta de primera ejecución
  que analiza en una máquina virtual lo descargado antes de dejarlo ejecutar), cortafuegos nftables, DNS
  cifrado sin rastreadores, USB desconocidos bloqueados con la sesión bloqueada, AppArmor, kernel endurecido
  con lockdown y módulos firmados, cifrado del disco en el instalador, copias e instantáneas btrfs.
- **Rendimiento**: Centinela adapta el sistema al equipo (CPU, memoria, disco, gráfica, batería): zram con
  recompresión, compresión anticipada de la memoria de las apps en segundo plano, planificador por disco,
  ahorro de energía sin perder funciones, compatibilidad gráfica (GPU AMD GCN 1/2 con Vulkan, WineD3D/Lavapipe
  en GPU sin Vulkan), TRIM a través del cifrado y aviso de desgaste del SSD.
- **Ajustes medidos, no supuestos**: `sudo centinela comparar probar <ajuste>` activa un ajuste, mide en rondas
  A-B-B-A con una prueba estadística y solo lo deja si gana en ESE equipo (`centinela comparar lista`). Con
  varios kernels instalados, cada arranque se mide solo y `centinela comparar kernels` dice cuál rinde más.
- **Modo IA**: mientras TISMA Code genera con el motor local, el escritorio quita los efectos que releen la
  pantalla, Baloo se pausa y el sistema detiene KSM, la compactación y la recompresión de memoria, para dejarle
  a la IA el ancho de banda (clave en equipos con gráfica integrada). Medible con
  `centinela comparar probar modo-ia --modelo X.gguf`; si en tu equipo empeora, se apaga solo.
- **Mantenimiento con el equipo libre**: análisis de rootkits, auditoría, scrub, TRIM, copias externas y
  actualizaciones de Flatpak esperan a que el equipo esté enchufado y sin uso (sin saltarse: tras 12 h se hacen
  igual si la batería lo permite).
- **IA local (TISMA Code)**: agente de programación, cálculo, CAD y simulación con el motor que mejor rinde en
  tu equipo (Strata o llama.cpp), herramientas por kits, cuaderno de trabajo y reinicio limpio del contexto
  para modelos pequeños, herramientas de ingeniería con plantillas y comprobación de resultados.
- **Aplicaciones**: en la ISO, la base (multimedia, navegador, PDF, escritorio remoto, Tienda con Flathub, Mesa y
  firmware recientes de backports). **LibreOffice** (en español) se descarga solo **al instalar** TISMA OS en el
  disco; si no había conexión, en el primer arranque con red. El resto se descarga **al elegir tu perfil** en la
  Bienvenida (o con `sudo tisma-perfil instalar <perfil>`), desde los repositorios firmados de Debian:
  - *diario / estudio*: correo (Thunderbird), finanzas, informes con LaTeX;
  - *creación*: GIMP, Inkscape, Krita, Blender, Kdenlive, OBS, Audacity;
  - *ingeniería*: FreeCAD, KiCad, OpenSCAD, gmsh, ngspice, Octave, Verilog, PrusaSlicer, Jupyter…;
  - *juegos*: Steam, Wine, MangoHud, GameMode y las bibliotecas de 32 bits;
  - *programación*: compiladores (clang, Rust), linters, Wireshark CLI, REA.

  Strata (solo útil con GPU dedicada y ≥32 GB), REA y Craft Anything se descargan de GitHub con versión y
  SHA-256 fijados (`sudo centinela-apps strata|rea|craft`); TISMA Code los propone cuando sirven.

## Cómo probarla

1. **En una máquina virtual** (lo más seguro): QEMU/virt-manager o VirtualBox con UEFI, 4 GB de RAM o más y
   un disco de 40 GB.
2. **En un equipo real**: graba la ISO en un USB (`dd` o Ventoy) y arranca desde él: el instalador se abre solo.
   Para probar sin instalar, pulsa «Cancelar» (o Esc al arrancar > «Probar TISMA OS sin instalar»).
   **Secure Boot puede quedarse activado**.

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
- **Kernel TISMA**: esta preview trae el kernel TISMA `minimal` (pensado para máquinas virtuales) como
  opción; en hardware real arranca el kernel firmado de Debian, con todos los drivers. El kernel TISMA
  completo (`make kernel`, perfil `generic`) llegará en la siguiente.
- **Apps por perfil**: necesitan conexión a Internet para descargarse (la ISO pesa mucho menos a cambio). En
  la sesión live no se instalan (irían a la RAM): se eligen tras instalar en el disco. Para instalar sin
  conexión se construye la ISO con `TISMA_ISO_COMPLETA=1`.
- **Endurecido como Kicksecure en lo que no cuesta nada**: tras 10 contraseñas fallidas seguidas la cuenta se
  bloquea 10 minutos (también en la pantalla de bloqueo; un administrador la desbloquea con
  `sudo faillock --user <usuario> --reset`); `su` solo para administradores; sin programas de 32 bits hasta
  instalar el perfil de juegos (Steam y Wine los necesitan: el perfil los activa y pide reiniciar).
- **Más liviana a cambio de**: la sesión live no trae LibreOffice (se descarga al instalar) y arranca algo más
  lenta (squashfs con xz, para que la ISO quepa en un archivo de GitHub); solo traducciones al español (y al inglés), sin documentación de paquetes (las
  licencias sí), sin fuentes chinas/japonesas/coreanas, sin 32 bits hasta instalar el perfil de juegos, y el
  firmware de NVIDIA se instala al detectar la GPU (`tisma-drivers`). La máquina virtual de análisis de Escudo
  solo va en la ISO completa: en la normal, lo descargado se analiza en su sandbox.
- **Hope va incluido** (sus herramientas de cálculo exacto, crítico y consejo; no la red neuronal), desde una
  versión fija y comprobada de su repositorio público.
- **Componentes compilados**: una ISO construida con `TISMA_SIN_COMPILAR=1` no trae hardened_malloc,
  planificadores sched_ext ni el motor ML de Escudo. llama.cpp sí (build oficial con Vulkan y todas las CPU).
- Sin probar todavía en hardware real: la compatibilidad gráfica (cambio a amdgpu en GCN 1/2), la compresión
  de memoria de apps (cgroups reales), Waydroid, Proton y los límites de potencia.

## Informar de fallos

Abre una incidencia en https://github.com/adriangrochi-code/antivirus/issues con: modelo del equipo, qué
hacías, qué esperabas y qué pasó, y la salida de `journalctl -b -p warning` (revísala antes de pegarla: puede
contener nombres de archivos o redes).
