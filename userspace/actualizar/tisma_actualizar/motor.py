"""Actualiza un TISMA instalado con el sistema del USB, sin tocar /home y conservando la configuración.

Solo funciona en la sesión del USB (boot=live) y solo sobre una partición del instalador de TISMA (etiqueta
«TISMA», con la de arranque «TISMA-arranque» en el mismo disco). Pasos:

1. Abre el disco cifrado con la contraseña (llega por la entrada estándar, nunca en la línea de órdenes).
2. Copia el sistema nuevo en un subvolumen aparte (@tisma-nuevo). El instalado NO se toca todavía.
3. Le pasa lo tuyo: cuentas y contraseñas, red y wifi, nombre del equipo, idioma y teclado, impresoras, USB
   autorizados, ajustes de TISMA, Flatpak y los datos de /var/lib, /root, /usr/local y /opt.
4. Cambia uno por otro: @ pasa a @tisma-anterior-FECHA (para volver atrás) y @tisma-nuevo pasa a @. Guarda una
   copia de la partición de arranque dentro del anterior.
5. Dentro del sistema nuevo: quita lo propio del USB (instalador, live-boot), genera los idiomas, el arranque
   (initramfs y GRUB) y deja apuntados los programas que tenías y la versión nueva no trae, para instalarlos
   al primer arranque con internet.
Si algo falla en el paso 5, se deshace el cambio: el equipo arranca como antes.

Progreso: una línea JSON por paso en la salida estándar ({"paso", "texto", "pct"} o {"error"} / {"listo"}).
"""
from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import usuarios

MAPEO = "tisma-actualizar"
FUENTE_LIVE = "/run/live/rootfs/filesystem.squashfs"
NUEVO, ACTUAL, ANTERIOR = "@tisma-nuevo", "@", "@tisma-anterior-"
RESPALDO_BOOT = "tisma-respaldo-boot"            # dentro del subvolumen anterior

# Lo que es de ESTE equipo y se conserva (relativo a /etc). Los directorios se suman (no se borra lo nuevo).
ETC_CONSERVAR = [
    "fstab", "crypttab", "hostname", "hosts", "machine-id", "adjtime", "localtime", "timezone", "hostid",
    "default/locale", "locale.gen", "locale.conf", "default/keyboard", "vconsole.conf", "console-setup",
    "X11/xorg.conf.d", "sudoers.d", "NetworkManager/system-connections", "ssh/ssh_host_*", "sddm.conf",
    "sddm.conf.d", "usbguard/rules.conf", "default/grub", "initramfs-tools/conf.d", "cups", "subuid", "subgid",
    "apt/sources.list", "apt/sources.list.d", "cryptsetup-initramfs", "cryptsetup-keys.d",
]
ETC_SOLO_SI_FALTA = ["tisma"]                    # ajustes elegidos (perfiles, copias…), sin pisar los de fábrica
# /var/lib: todo lo del equipo menos el registro de paquetes (es el de la versión nueva) y lo que se regenera
VARLIB_EXCLUIR = ["dpkg", "apt", "ucf", "initramfs-tools", "python", "systemd/deb-systemd-helper-enabled",
                  "systemd/deb-systemd-user-helper-enabled", "dkms"]
# Lo que el instalador quita al instalar (modules/packages.conf): es solo del USB
PAQUETES_LIVE = ["live-boot", "live-boot-doc", "live-boot-initramfs-tools", "live-config", "live-config-doc",
                 "live-config-systemd", "live-tools", "live-task-localisation", "live-task-recommended",
                 "user-setup", "calamares-settings-debian"]
PENDIENTES = "var/lib/tisma/reinstalar.list"
RSYNC = ["rsync", "-aHAXx", "--numeric-ids"]
NO_COPIAR = ["/proc/*", "/sys/*", "/dev/*", "/run/*", "/tmp/*", "/home/*", "/var/log/*", "/var/cache/*",
             "/.snapshots/*", "/media/*", "/mnt/*"]


class Fallo(Exception):
    """Error con un texto para la persona (sin tecnicismos)."""


def aviso(paso: str, texto: str, pct: int | None = None, salida=sys.stdout) -> None:
    salida.write(json.dumps({"paso": paso, "texto": texto, "pct": pct}, ensure_ascii=False) + "\n")
    salida.flush()


def ejecutar(argv: list[str], entrada: bytes | None = None, raiz: str | None = None, **kw) -> str:
    if raiz:
        argv = ["chroot", raiz, *argv]
    r = subprocess.run(argv, input=entrada, capture_output=True, **kw)
    if r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, argv, r.stdout, r.stderr)
    return r.stdout.decode("utf-8", "replace") if isinstance(r.stdout, bytes) else (r.stdout or "")


# ---------------------------------------------------------------- buscar el sistema instalado

def candidatos(lsblk: dict, medio_live: str | None) -> list[dict]:
    """Discos con un TISMA instalado (particiones «TISMA» + «TISMA-arranque»), sin contar el propio USB."""
    salida = []
    for disco in lsblk.get("blockdevices", []):
        if medio_live and disco.get("name") == medio_live:
            continue
        hijos = disco.get("children") or []
        raiz = next((h for h in hijos if h.get("partlabel") == "TISMA"), None)
        boot = next((h for h in hijos if h.get("partlabel") == "TISMA-arranque"), None)
        esp = next((h for h in hijos if (h.get("parttype") or "").lower() == "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"),
                   None)
        if raiz and boot:
            salida.append({"disco": "/dev/" + disco["name"], "raiz": "/dev/" + raiz["name"],
                           "arranque": "/dev/" + boot["name"], "esp": "/dev/" + esp["name"] if esp else None,
                           "cifrado": raiz.get("fstype") == "crypto_LUKS",
                           "modelo": (disco.get("model") or "").strip(), "tamano": disco.get("size")})
    return salida


def disco_del_usb() -> str | None:
    """Nombre del disco desde el que arrancó la sesión live (no se ofrece como destino)."""
    try:
        for linea in open("/proc/mounts"):
            dev, punto = linea.split()[:2]
            if punto == "/run/live/medium" and dev.startswith("/dev/"):
                r = subprocess.run(["lsblk", "-no", "PKNAME", dev], capture_output=True, text=True)
                return (r.stdout.strip() or os.path.basename(dev)).splitlines()[0]
    except OSError:
        pass
    return None


def buscar() -> list[dict]:
    r = subprocess.run(["lsblk", "-J", "-o", "NAME,PARTLABEL,PARTTYPE,FSTYPE,MODEL,SIZE,TYPE"],
                       capture_output=True, text=True)
    return candidatos(json.loads(r.stdout or "{}"), disco_del_usb())


# ---------------------------------------------------------------- qué conservar

def paquetes_instalados(status: str) -> dict[str, str]:
    """Paquetes instalados en un /var/lib/dpkg/status → {nombre: arquitectura}."""
    salida = {}
    for bloque in status.split("\n\n"):
        campos = dict(re.findall(r"^([A-Za-z-]+): ?(.*)$", bloque, re.M))
        if campos.get("Status", "").endswith(" installed") and campos.get("Package"):
            salida[campos["Package"]] = campos.get("Architecture", "")
    return salida


def automaticos(extended_states: str) -> set[str]:
    salida, nombre = set(), None
    for linea in extended_states.splitlines():
        if linea.startswith("Package:"):
            nombre = linea.split(":", 1)[1].strip()
        elif linea.startswith("Auto-Installed:") and linea.split(":", 1)[1].strip() == "1" and nombre:
            salida.add(nombre)
    return salida


def a_reinstalar(viejo: str, nuevo: str) -> list[str]:
    """Programas que se habían instalado a mano (Tienda, perfiles) y la versión nueva no trae."""
    def leer(raiz, ruta):
        try:
            return open(os.path.join(raiz, ruta), encoding="utf-8", errors="replace").read()
        except OSError:
            return ""
    antes = paquetes_instalados(leer(viejo, "var/lib/dpkg/status"))
    ahora = paquetes_instalados(leer(nuevo, "var/lib/dpkg/status"))
    auto = automaticos(leer(viejo, "var/lib/apt/extended_states"))
    salida = []
    for nombre, arq in sorted(antes.items()):
        if nombre in auto or nombre in ahora or nombre in PAQUETES_LIVE or nombre.startswith("linux-image-"):
            continue
        salida.append(f"{nombre}:{arq}" if arq == "i386" else nombre)
    return salida


def rutas_etc(raiz_vieja: str) -> list[str]:
    """Rutas de ETC_CONSERVAR que existen en el sistema instalado (con comodines resueltos)."""
    etc = os.path.join(raiz_vieja, "etc")
    salida = []
    for patron in ETC_CONSERVAR:
        carpeta, base = os.path.split(patron)
        try:
            nombres = os.listdir(os.path.join(etc, carpeta))
        except OSError:
            continue
        salida += [os.path.join(carpeta, n) for n in sorted(nombres) if fnmatch.fnmatchcase(n, base)]
    return salida


def puntos_fstab(fstab: str) -> list[tuple[str, str]]:
    """(punto de montaje o archivo de swap, tipo) de cada línea de /etc/fstab."""
    salida = []
    for linea in fstab.splitlines():
        campos = linea.split()
        if len(campos) >= 3 and not campos[0].startswith("#"):
            if campos[2] == "swap":
                if campos[0].startswith("/") and not campos[0].startswith("/dev/"):
                    salida.append((campos[0], "swap"))
            elif campos[1].startswith("/"):
                salida.append((campos[1], campos[2]))
    return salida


# ---------------------------------------------------------------- el trabajo

class Actualizacion:
    def __init__(self, destino: dict, fuente: str = FUENTE_LIVE, salida=sys.stdout, run=ejecutar):
        self.d, self.fuente, self.salida, self.run = destino, fuente, salida, run
        self.w = tempfile.mkdtemp(prefix="tisma-actualizar-")
        self.top, self.raiz = os.path.join(self.w, "top"), os.path.join(self.w, "raiz")
        self.montajes: list[str] = []
        self.fecha = time.strftime("%Y%m%d-%H%M")

    def avisar(self, paso, texto, pct=None):
        aviso(paso, texto, pct, self.salida)

    def montar(self, dev, punto, *opciones):
        os.makedirs(punto, exist_ok=True)
        self.run(["mount", *opciones, dev, punto])
        self.montajes.append(punto)

    def desmontar_todo(self):
        for punto in reversed(self.montajes):
            subprocess.run(["umount", "-R", punto], capture_output=True)
        self.montajes.clear()

    # 1
    def abrir(self, clave: bytes | None) -> str:
        if not self.d["cifrado"]:
            return self.d["raiz"]
        if os.path.exists(f"/dev/mapper/{MAPEO}"):
            subprocess.run(["cryptsetup", "close", MAPEO], capture_output=True)
        try:
            self.run(["cryptsetup", "open", "--type", "luks", "--key-file=-", self.d["raiz"], MAPEO], entrada=clave)
        except subprocess.CalledProcessError:
            raise Fallo("La contraseña del disco no es correcta.")
        return f"/dev/mapper/{MAPEO}"

    def comprobar(self):
        actual = os.path.join(self.top, ACTUAL)
        rel = os.path.join(actual, "etc/os-release")
        if not os.path.isfile(rel) or "TISMA" not in open(rel, errors="replace").read():
            raise Fallo("No encontré TISMA instalado en ese disco.")
        libre = os.statvfs(self.top)
        necesita = os.statvfs(self.fuente)
        if libre.f_bavail * libre.f_frsize < (necesita.f_blocks * necesita.f_frsize) * 1.2 + 2e9:
            raise Fallo("No hay espacio libre suficiente en el disco para actualizar. Libera espacio "
                        "(por ejemplo, vaciando la Papelera) y vuelve a intentarlo.")

    # 2
    def copiar_sistema(self):
        nuevo = os.path.join(self.top, NUEVO)
        if os.path.exists(nuevo):
            subprocess.run(["btrfs", "subvolume", "delete", "-R", nuevo], capture_output=True)
            subprocess.run(["btrfs", "subvolume", "delete", nuevo], capture_output=True)
        self.run(["btrfs", "subvolume", "create", nuevo])
        argv = [*RSYNC, "--info=progress2", "--no-inc-recursive", *[f"--exclude={x}" for x in NO_COPIAR],
                self.fuente + "/", nuevo + "/"]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        ultimo = -1
        for trozo in iter(lambda: proc.stdout.read(256), ""):
            m = re.findall(r"(\d+)%", trozo)
            if m and int(m[-1]) != ultimo:
                ultimo = int(m[-1])
                self.avisar("copiar", "Copiando el sistema nuevo…", 10 + ultimo * 55 // 100)
        if proc.wait() != 0:
            raise RuntimeError("rsync: " + proc.stderr.read()[-500:])

    # 3
    def pasar_lo_tuyo(self):
        viejo, nuevo = os.path.join(self.top, ACTUAL), os.path.join(self.top, NUEVO)
        # Cuentas
        leer = lambda r, f: usuarios.leer(open(os.path.join(r, "etc", f), errors="replace").read())
        passwd_viejo = leer(viejo, "passwd")
        passwd, mapa_u = usuarios.unir(passwd_viejo, leer(nuevo, "passwd"))
        group, mapa_g = usuarios.unir(leer(viejo, "group"), leer(nuevo, "group"), grupos=True)
        anadidas = {f[0] for f in passwd} - {f[0] for f in passwd_viejo}
        passwd = usuarios.gid_principal(passwd, mapa_g, anadidas)
        nombres_u, nombres_g = {f[0] for f in passwd}, {f[0] for f in group}
        shadow = usuarios.unir_sombra(leer(viejo, "shadow"), leer(nuevo, "shadow"), nombres_u)
        gshadow = usuarios.unir_sombra(leer(viejo, "gshadow"), leer(nuevo, "gshadow"), nombres_g)
        n = usuarios.renumerar(nuevo, usuarios.solo_cambios(mapa_u), usuarios.solo_cambios(mapa_g))
        for nombre, filas, modo in (("passwd", passwd, 0o644), ("group", group, 0o644), ("shadow", shadow, 0o640),
                                    ("gshadow", gshadow, 0o640)):
            ruta = os.path.join(nuevo, "etc", nombre)
            st = os.stat(ruta)
            with open(ruta, "w") as f:
                f.write(usuarios.escribir(filas))
            os.chmod(ruta, modo)
            os.chown(ruta, 0, st.st_gid)
        self.avisar("tuyo", f"Cuentas listas ({n} archivos renumerados)", 70)
        # /etc
        for rel in rutas_etc(viejo):
            origen = os.path.join(viejo, "etc", rel)
            destino = os.path.join(nuevo, "etc", rel)
            os.makedirs(os.path.dirname(destino), exist_ok=True)
            if os.path.isdir(origen) and not os.path.islink(origen):
                self.run([*RSYNC, origen + "/", destino + "/"])
            else:
                self.run([*RSYNC, origen, destino])
        self.puntos_y_swap(viejo, nuevo)
        for rel in ETC_SOLO_SI_FALTA:
            origen = os.path.join(viejo, "etc", rel)
            if os.path.isdir(origen):
                self.run([*RSYNC, "--ignore-existing", origen + "/", os.path.join(nuevo, "etc", rel) + "/"])
        # Datos del sistema
        if os.path.isdir(os.path.join(viejo, "var/lib")):
            self.run([*RSYNC, *[f"--exclude=/{x}" for x in VARLIB_EXCLUIR],
                      os.path.join(viejo, "var/lib") + "/", os.path.join(nuevo, "var/lib") + "/"])
        for rel, nuevos_ganan in (("root", False), ("usr/local", True), ("opt", True), ("srv", False)):
            origen = os.path.join(viejo, rel)
            if os.path.isdir(origen):
                self.run([*RSYNC, *(["--ignore-existing"] if nuevos_ganan else []),
                          origen + "/", os.path.join(nuevo, rel) + "/"])
        # Programas que tenías y la versión nueva no trae (se instalan al primer arranque con internet)
        lista = a_reinstalar(viejo, nuevo)
        arq = os.path.join(viejo, "var/lib/dpkg/arch")
        if lista or os.path.exists(arq):
            ruta = os.path.join(nuevo, PENDIENTES)
            os.makedirs(os.path.dirname(ruta), exist_ok=True)
            with open(ruta, "w") as f:
                f.write("".join(p + "\n" for p in lista))
            if os.path.exists(arq) and "i386" in open(arq).read():
                open(os.path.join(nuevo, "var/lib/tisma/arquitecturas"), "w").write("i386\n")
        with open(os.path.join(nuevo, "etc/tisma/actualizado"), "w") as f:
            f.write(f"{self.fecha}\n")
        self.avisar("tuyo", "Tus ajustes y programas, conservados", 75)

    def puntos_y_swap(self, viejo: str, nuevo: str):
        """Las carpetas donde se montan los otros subvolúmenes (/home, /.snapshots…) tienen que existir en el sistema
        nuevo o el arranque se para; el archivo de intercambio, si estaba dentro del sistema, se vuelve a crear."""
        for punto, tipo in puntos_fstab(open(os.path.join(nuevo, "etc/fstab"), errors="replace").read()):
            if tipo == "swap":
                if os.path.isfile(os.path.join(viejo, punto.lstrip("/"))) and \
                        not os.path.exists(os.path.join(nuevo, punto.lstrip("/"))):
                    tamano = os.path.getsize(os.path.join(viejo, punto.lstrip("/")))
                    os.makedirs(os.path.dirname(os.path.join(nuevo, punto.lstrip("/"))), exist_ok=True)
                    self.run(["btrfs", "filesystem", "mkswapfile", "--size", f"{tamano // 1048576}m",
                              os.path.join(nuevo, punto.lstrip("/"))])
            elif punto != "/":
                os.makedirs(os.path.join(nuevo, punto.lstrip("/")), exist_ok=True)

    # 4
    def cambiar(self):
        anterior = os.path.join(self.top, ANTERIOR + self.fecha)
        os.rename(os.path.join(self.top, ACTUAL), anterior)
        os.rename(os.path.join(self.top, NUEVO), os.path.join(self.top, ACTUAL))
        self.anterior = anterior

    def deshacer(self):
        """Vuelve a dejar el sistema como estaba (si ya se había hecho el cambio)."""
        anterior = getattr(self, "anterior", None)
        if not anterior or not os.path.isdir(anterior):
            return
        self.desmontar_todo()
        self.montar(self.dev, self.top, "-o", "subvolid=5")
        actual = os.path.join(self.top, ACTUAL)
        if os.path.isdir(actual):
            os.rename(actual, os.path.join(self.top, "@tisma-fallido-" + self.fecha))
        os.rename(anterior, actual)
        respaldo = os.path.join(actual, RESPALDO_BOOT)
        if os.path.isdir(respaldo):
            boot = os.path.join(self.w, "boot")
            self.montar(self.d["arranque"], boot)
            self.run([*RSYNC, "--delete", "--exclude=/efi", respaldo + "/", boot + "/"])
            shutil.rmtree(respaldo, ignore_errors=True)

    # 5
    def preparar_arranque(self):
        raiz = self.raiz
        self.montar(self.dev, raiz, "-o", f"subvol=/{ACTUAL}")
        boot = os.path.join(raiz, "boot")
        kernels_nuevos = os.path.join(self.w, "kernels")
        # El /boot que trae la imagen (kernels nuevos) queda tapado al montar la partición de arranque: se guarda
        self.run([*RSYNC, boot + "/", kernels_nuevos + "/"])
        self.montar(self.d["arranque"], boot)
        self.run([*RSYNC, "--delete", "--exclude=/efi", "--exclude=/lost+found", boot + "/",
                  os.path.join(self.anterior, RESPALDO_BOOT) + "/"])
        modulos = set(os.listdir(os.path.join(raiz, "lib/modules")))
        for nombre in os.listdir(boot):        # kernels viejos: sus módulos ya no están en el sistema nuevo
            m = re.match(r"(vmlinuz|initrd\.img|System\.map|config)-(.+)$", nombre)
            if m and m.group(2) not in modulos:
                os.unlink(os.path.join(boot, nombre))
        self.run([*RSYNC, "--exclude=/efi", "--exclude=/grub", kernels_nuevos + "/", boot + "/"])
        if self.d.get("esp") and os.path.isdir(os.path.join(boot, "efi")):
            self.montar(self.d["esp"], os.path.join(boot, "efi"))
        for fs, punto, opc in (("/dev", "dev", ["--rbind"]), ("/proc", "proc", ["--bind"]),
                               ("/sys", "sys", ["--rbind"]), ("/run", "run", ["--bind"])):
            self.montar(fs, os.path.join(raiz, punto), *opc)
        entorno = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DEBIAN_FRONTEND": "noninteractive", "LANG": "C.UTF-8"}
        instalados = paquetes_instalados(open(os.path.join(raiz, "var/lib/dpkg/status"), errors="replace").read())
        quitar = [p for p in PAQUETES_LIVE if p in instalados]
        if quitar:
            self.avisar("arranque", "Quitando lo que solo sirve en el USB…", 80)
            self.run(["dpkg", "--purge", "--force-depends", *quitar], raiz=raiz, env=entorno)
        self.avisar("arranque", "Preparando idiomas…", 84)
        subprocess.run(["chroot", raiz, "locale-gen"], capture_output=True, env=entorno)
        self.avisar("arranque", "Preparando el arranque…", 88)
        for version in sorted(modulos):
            accion = "-u" if os.path.exists(os.path.join(boot, f"initrd.img-{version}")) else "-c"
            self.run(["update-initramfs", accion, "-k", version], raiz=raiz, env=entorno)
        subprocess.run(["chroot", raiz, "/usr/bin/tisma-secureboot", "elegir"], capture_output=True, env=entorno)
        self.run(["update-grub"], raiz=raiz, env=entorno)

    def limpiar_anteriores(self, conservar: int = 1):
        """Solo se guarda la última versión anterior (cada una ocupa varios GB)."""
        viejas = sorted(n for n in os.listdir(self.top) if n.startswith(ANTERIOR))
        for n in viejas[:-conservar]:
            subprocess.run(["btrfs", "subvolume", "delete", os.path.join(self.top, n)], capture_output=True)
        for n in os.listdir(self.top):
            if n.startswith("@tisma-fallido-"):
                subprocess.run(["btrfs", "subvolume", "delete", os.path.join(self.top, n)], capture_output=True)

    def hacer(self, clave: bytes | None):
        try:
            self.avisar("abrir", "Abriendo el disco…", 2)
            self.dev = self.abrir(clave)
            self.montar(self.dev, self.top, "-o", "subvolid=5")
            self.comprobar()
            self.avisar("copiar", "Copiando el sistema nuevo…", 10)
            self.copiar_sistema()
            self.avisar("tuyo", "Pasando tus cuentas, wifi y ajustes…", 66)
            self.pasar_lo_tuyo()
            self.cambiar()
            try:
                self.preparar_arranque()
            except Exception:
                self.deshacer()
                raise
            self.desmontar_todo()
            self.montar(self.dev, self.top, "-o", "subvolid=5")
            self.limpiar_anteriores()
            self.avisar("listo", "¡Listo! Quita el USB y reinicia el equipo.", 100)
        finally:
            self.desmontar_todo()
            if self.d["cifrado"]:
                subprocess.run(["cryptsetup", "close", MAPEO], capture_output=True)
            shutil.rmtree(self.w, ignore_errors=True)

    def volver(self, clave: bytes | None):
        """Botón «Volver a la versión anterior»: cambia @ por la última @tisma-anterior-* y su arranque."""
        try:
            self.dev = self.abrir(clave)
            self.montar(self.dev, self.top, "-o", "subvolid=5")
            viejas = sorted(n for n in os.listdir(self.top) if n.startswith(ANTERIOR))
            if not viejas:
                raise Fallo("No hay una versión anterior guardada.")
            self.anterior = os.path.join(self.top, viejas[-1])
            self.deshacer()
            self.avisar("listo", "Listo: al reiniciar tendrás la versión anterior.", 100)
        finally:
            self.desmontar_todo()
            if self.d["cifrado"]:
                subprocess.run(["cryptsetup", "close", MAPEO], capture_output=True)
            shutil.rmtree(self.w, ignore_errors=True)


def main(argv=None) -> int:
    """Uso (como root, en la sesión del USB): actualizar-instalado buscar | actualizar <raiz> | volver <raiz>.
    La contraseña del disco llega por la entrada estándar."""
    argv = sys.argv[1:] if argv is None else argv
    if "boot=live" not in open("/proc/cmdline").read().split():
        print(json.dumps({"error": "Esto solo se puede hacer arrancando desde el USB de TISMA."}))
        return 2
    if not argv or argv[0] == "buscar":
        print(json.dumps({"discos": buscar()}, ensure_ascii=False))
        return 0
    orden, raiz = argv[0], (argv[1] if len(argv) > 1 else "")
    destino = next((c for c in buscar() if c["raiz"] == raiz), None)
    if orden not in ("actualizar", "volver") or not destino:
        print(json.dumps({"error": "No encontré TISMA instalado en ese disco."}, ensure_ascii=False))
        return 2
    clave = sys.stdin.buffer.readline().rstrip(b"\n") if destino["cifrado"] else None
    if orden == "actualizar" and not os.path.isdir(FUENTE_LIVE):
        print(json.dumps({"error": "No encuentro el sistema del USB."}, ensure_ascii=False))
        return 2
    a = Actualizacion(destino)
    try:
        (a.hacer if orden == "actualizar" else a.volver)(clave)
    except Fallo as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False), flush=True)
        return 1
    except Exception as e:      # noqa: BLE001 - se informa y el sistema queda como estaba
        sys.stderr.write(f"{type(e).__name__}: {e}\n")
        print(json.dumps({"error": "No se pudo actualizar. Tu sistema quedó como estaba."}, ensure_ascii=False),
              flush=True)
        return 1
    return 0
