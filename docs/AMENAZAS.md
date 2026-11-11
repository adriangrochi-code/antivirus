# Modelo de amenazas de TISMA OS

Basado en datos de 2025–2026: la **explotación de vulnerabilidades** es la vía de entrada nº 1 (31 %, Verizon
DBIR 2026), seguida del **phishing** (16 %) y el **abuso de credenciales** (13 %); en Linux, el **89 %** de la
actividad maliciosa observada es **fuerza bruta contra SSH** (Elastic); los **ladrones de información** llegan
sobre todo en programas crackeados, descargas y adjuntos; y la cadena de suministro (caso xz) es un riesgo real.

Cada fila: el ataque, las capas que lo frenan (defensa en profundidad) y el riesgo que queda.

| # | Ataque | Defensa en TISMA OS | Riesgo residual |
|---|---|---|---|
| 1 | **Explotar software sin parchear** | Kernel LTS con firma verificada; aviso diario de parche nuevo (`tisma-kernel-estado`); `make cve-check` desactiva subsistemas prescindibles con CVEs; actualizaciones automáticas de Debian; superficie reducida (io_uring, BPF sin privilegios, módulos raros bloqueados, lockdown) | El kernel a medida se actualiza recompilando: hay que aceptar el aviso |
| 2 | **Fuerza bruta SSH / servicios expuestos** | Ningún servicio escuchando por defecto; firewall que lo bloquea todo al entrar; sshd (si se instala) solo con llaves y 3 intentos; Escudo bloquea IPs atacantes (patrones anclados, no falsificables) | Servicios que el usuario abra a mano |
| 3 | **Phishing: adjuntos y documentos maliciosos** | LibreOffice y visores **sin red** en su sandbox (las macros no pueden exfiltrar); Escudo escanea Descargas (firmas, YARA, ML); alerta al ejecutar algo desde Descargas o /tmp; navegador bloquea descargas peligrosas | El usuario puede aprobar algo malicioso |
| 4 | **Ladrones de información** (cookies, contraseñas, llaves SSH, carteras) | Cada app ve solo sus carpetas (no ve `~/.ssh` ni el perfil del navegador); auditd alerta si un proceso no autorizado lee llaves, datos del navegador, carteras, tokens de Discord/Telegram o credenciales de nube | Malware que ya corra como el usuario fuera del sandbox puede leer antes de ser detectado (se detecta, no siempre se impide) |
| 5 | **Programas crackeados / de Windows** | Wine en sandbox **sin red** y con su propia carpeta; Escudo con modelo EMBER2024 para PE | Un crack puede seguir siendo malware dentro de su sandbox |
| 6 | **Ransomware** | Detector de cifrado masivo que **congela** el proceso; sandbox limita qué carpetas ve cada app | Instantáneas btrfs de solo lectura (diarias y antes de cada actualización) + copia externa cifrada con restic: se restaura lo cifrado | Un atacante con root puede borrar las instantáneas: la copia externa desconectada es la que lo cubre |
| 7 | **Escalada de privilegios local** | KSPP (init_on_free, aleatorización, hardened usercopy…), Yama ptrace, módulos firmados, `tisma-seguridad suid`, servicios con sandbox de systemd, carreras con symlinks corregidas en Escudo | Espacios de nombres sin privilegios siguen permitidos (los necesitan bubblewrap y los navegadores) |
| 8 | **Navegador: exploits y rastreo** | Chromium con aislamiento por sitio, modo blindado sin JIT, cookies de terceros bloqueadas, WebRTC sin fuga de IP, APIs de hardware bloqueadas, sandbox propio | Día cero en el motor del navegador |
| 9 | **Redes Wi-Fi públicas (MITM, rastreo)** | DNS cifrado con DNSSEC; solo HTTPS; VPN WireGuard con kill-switch; MAC aleatoria por red; IPv6 con direcciones temporales | Sin VPN, los metadatos de conexión siguen visibles |
| 10 | **Acceso físico / portátil robado** | Cifrado LUKS2 en el instalador; USBGuard (`tisma-seguridad usb`); FireWire bloqueado; IOMMU estricto contra DMA | **Secure Boot aún no** (falta shim/UKI firmado) |
| 11 | **Cadena de suministro** | Kernel con firma PGP fijada; Debian verificado desde InRelease; Strata, Tisma engine, Node y REA con hash fijado; Hope y OpenJarvis en commit fijado; npm sin scripts de instalación | Una dependencia comprometida *antes* de fijarla |
| 12 | **Inyección de instrucciones a la IA** (repos, documentos o webs maliciosos) | TISMA Code: cada escritura/comando pide permiso; comandos sin red ni `$HOME`; Hope y REA aislados; fuentes marcadas como no verificadas; el motor local debe ser del usuario | El usuario puede aprobar una acción dañina |
| 13 | **Criptomineros / abuso de recursos** | Patrones de Escudo (stratum), adaptd ve la presión de CPU | Detección por comportamiento sostenido aún básica |

## Pendientes priorizados
1. **Secure Boot** con UKI firmado y llave propia (MOK), que además acelera el arranque.
2. Restringir espacios de nombres sin privilegios por perfil de AppArmor (como Ubuntu 24.04), permitiéndolos solo
   a bubblewrap y a los navegadores.
3. Detección de mineros por consumo sostenido de CPU de procesos desconocidos.
