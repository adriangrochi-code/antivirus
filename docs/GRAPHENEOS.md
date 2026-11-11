# Qué tomamos de GrapheneOS

Revisión de https://grapheneos.org/features y de hardened_malloc (noviembre de 2026), trasladada a un
escritorio Linux x86_64. Medidas ya presentes en TISMA antes de esta revisión (endurecimiento KSPP, lockdown,
LSM, `init_on_free`, ASLR máximo, sandbox por app, red por app, kill-switch de la VPN sin fugas de
multicast…) no se repiten aquí.

## Adoptado (activo por defecto)

| GrapheneOS | En TISMA OS | Coste |
|---|---|---|
| JIT restringido en Vanadium | Chromium sin los **optimizadores de V8** (`DefaultJavaScriptOptimizerSetting=2`); por sitio: `tisma-navegador-optimizador permitir <dominio>` | JS de cálculo intenso más lento |
| USB nuevos bloqueados con el equipo bloqueado | `tisma-bloqueo-sesion` + USBGuard: con la pantalla bloqueada, todo USB conectado queda bloqueado | Lo conectado durante el bloqueo hay que reconectarlo |
| MAC aleatoria por conexión | Wi-Fi con MAC nueva en cada conexión y DHCP sin el nombre del equipo | Algún portal cautivo pide registrarse otra vez |
| Hora de red fiable | chrony con **NTS** (hora autenticada), 4 servidores, mínimo 2 de acuerdo | Ninguno |
| hardened_malloc | Variante *light* en los demonios que tratan datos de la red: sshd, NetworkManager, polkit, cups, avahi, wpa_supplicant, bluetoothd, ModemManager, usbguard | Pequeño; no se aplica de forma global (rompe Chromium/Electron, Flatpak lo ignora, Steam/Wine de 32 bits no pueden cargarlo) |

## Opcional

- **Reinicio automático** si nadie desbloquea en N horas (GrapheneOS: 18 h): `HORAS=` en
  `/etc/tisma/reinicio-auto.conf`. Tras reiniciar, el disco cifrado se cierra y las claves salen de la RAM.
- **Modo estricto** (`tisma-seguridad estricto`): ptrace solo para root (`ptrace_scope=2`) y lockdown
  `confidentiality`. No va por defecto porque inutiliza gdb/strace sin sudo y bpftrace/perf del kernel, que en
  un sistema de ingeniería se usan a diario.

## Pendiente

- Secure Boot con UKI firmado + TPM2 con PIN (`systemd-cryptenroll --tpm2-with-pin=yes`): el TPM limita los
  intentos, lo más parecido al Titan M2.
- `cryptsetup-suspend` (borra la clave LUKS de la RAM al suspender) y PIN de coacción
  (`cryptsetup-nuke-password`): disponibles en Debian, por evaluar su experiencia de uso.
- CFI del kernel (kCFI requiere compilar con Clang) e IBT/shadow stack de usuario (CPU con CET; la A275 no lo
  tiene).

## No trasladable

MTE/PAC/BTI (ARM), Titan M2 y su limitación de intentos en hardware, el arranque verificado inmutable de
Android, el modelo de apps y permisos de Android (Storage/Contact Scopes, Play en sandbox), el corte de USB a
nivel de hardware y el aislamiento del módem.
