"""escudo windows: revisar y limpiar un Windows apagado (XP, Vista, 7, 8, 8.1… y posteriores) desde TISMA.

Arrancando TISMA desde el USB, el Windows del disco no se está ejecutando: el malware no puede esconderse,
defenderse ni volver a escribirse mientras se analiza (lo que hacen los discos de rescate de los antivirus).

Qué revisa:
* Todos los puntos de inicio automático conocidos (lo que mira Autoruns): claves Run/RunOnce de la máquina y de
  cada usuario, Winlogon (Shell, Userinit, Notify, GinaDLL), «depuradores» de Image File Execution Options (la
  puerta trasera de sethc/utilman), SilentProcessExit, AppInit_DLLs, AppCertDlls, paquetes de LSA, BootExecute,
  servicios y drivers, extensiones del navegador y de Explorer (BHO, ShellServiceObjectDelayLoad), tareas
  programadas (XML de Vista+ y .job de XP) y carpetas de Inicio (siguiendo los accesos directos).
* Cada programa al que apuntan: firmas, YARA y el modelo EMBER2024 (entrenado con ejecutables de Windows).
* Lugares donde se esconde el malware (escritorio, descargas, temporales, AppData) o el disco entero.
* Debilidades que deja el malware o una mala configuración: archivo hosts manipulado, UAC, cortafuegos,
  Windows Update o Defender desactivados.

Limpieza (``--limpiar``, solo lo grave salvo ``--tambien-medias``): los archivos van a una cuarentena en el propio
disco de Windows (``C:\\TISMA-Cuarentena``, comprimidos y restaurables) y las entradas del registro se
desactivan con cambios mínimos, guardando antes una copia de cada colmena.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .regf import Editor, Hive, Key, RegfError

ORDEN = {"high": 3, "medium": 2, "low": 1, "info": 0}
NOMBRE_GRAV = {"high": "GRAVE", "medium": "SOSPECHOSO", "low": "A REVISAR", "info": "normal"}

EJECUTABLES = (".exe", ".com", ".scr", ".pif", ".bat", ".cmd", ".dll", ".ocx", ".cpl", ".sys", ".drv", ".vbs",
               ".vbe", ".js", ".jse", ".wsf", ".wsh", ".hta", ".ps1", ".msi", ".jar", ".lnk", ".url")
# Nombres de programas del sistema que el malware imita fuera de su carpeta
SISTEMA = {"svchost.exe", "csrss.exe", "lsass.exe", "lsm.exe", "winlogon.exe", "services.exe", "smss.exe",
           "spoolsv.exe", "taskhost.exe", "taskhostw.exe", "conhost.exe", "rundll32.exe", "explorer.exe",
           "wininit.exe", "dllhost.exe", "ctfmon.exe", "userinit.exe", "taskmgr.exe", "dwm.exe", "alg.exe"}
# Puertas traseras clásicas: estos programas se lanzan desde la pantalla de inicio de sesión, sin contraseña
ACCESIBILIDAD = {"sethc.exe", "utilman.exe", "osk.exe", "narrator.exe", "magnify.exe", "displayswitch.exe",
                 "atbroker.exe"}
ESCRIBIBLE = re.compile(r"\\(appdata|application data|local settings|configuración local|datos de programa|temp|"
                        r"programdata|all users\\application data|"
                        r"tmp|downloads|descargas|desktop|escritorio|users\\public|\$recycle\.bin|recycler|"
                        r"system volume information|windows\\temp|perflogs)\\", re.I)
DOBLE_EXT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|jpe?g|png|gif|txt|rtf|zip|rar|mp3|mp4|avi)\s*\.(exe|scr|com|pif|"
                       r"bat|cmd|vbs|js|hta)$", re.I)
LOLBIN = [
    (re.compile(r"powershell.*\s-(e|ec|enc|encodedcommand)\s", re.I), "high", "PowerShell con orden codificada"),
    (re.compile(r"powershell.*(downloadstring|downloadfile|invoke-webrequest|iwr |net\.webclient|"
                r"frombase64string|iex[ (]|invoke-expression)", re.I), "high", "PowerShell que descarga o "
                                                                              "ejecuta código"),
    (re.compile(r"powershell.*-w(indowstyle)?\s+h(idden)?", re.I), "medium", "PowerShell en ventana oculta"),
    (re.compile(r"mshta(\.exe)?\W.*(https?:|javascript:|vbscript:)", re.I), "high", "mshta con código remoto"),
    (re.compile(r"regsvr32(\.exe)?\W.*/i:\s*\S*(https?:|scrobj)", re.I), "high", "regsvr32 con script remoto "
                                                                              "(«Squiblydoo»)"),
    (re.compile(r"rundll32(\.exe)?\W.*(javascript:|mshtml,runhtmlapplication)", re.I), "high",
     "rundll32 ejecutando script"),
    (re.compile(r"certutil(\.exe)?\W.*(-urlcache|-decode)", re.I), "high", "certutil descargando o decodificando"),
    (re.compile(r"bitsadmin(\.exe)?\W.*/transfer", re.I), "high", "bitsadmin descargando"),
    (re.compile(r"\b(wscript|cscript)(\.exe)?\W", re.I), "medium", "script de Windows Script Host"),
    (re.compile(r"\bmshta(\.exe)?\W", re.I), "medium", "aplicación HTA (mshta)"),
]
CMD_INFORMATIVO = re.compile(r"^(\"?[a-z]:\\windows\\system32\\)?(rundll32|cmd|wscript|cscript|regsvr32|mshta|"
                             r"powershell)(\.exe)?\"?$", re.I)


@dataclass
class Hallazgo:
    tipo: str               # inicio, winlogon, ifeo, appinit, servicio, tarea, carpeta_inicio, archivo, …
    gravedad: str           # high | medium | low | info
    donde: str              # ubicación legible
    comando: str = ""
    archivo: str = ""       # ruta en este equipo (TISMA) del programa al que apunta
    motivos: list[str] = field(default_factory=list)
    detecciones: list[str] = field(default_factory=list)
    arreglo: dict | None = None     # {"colmena", "clave", "valor", "accion": borrar|cadena|dword, "dato"}
    usuario: str = ""
    sospechosos: list[str] = field(default_factory=list)   # archivos (rutas locales) que van a la cuarentena

    def subir(self, gravedad: str, motivo: str) -> None:
        if ORDEN[gravedad] > ORDEN[self.gravedad]:
            self.gravedad = gravedad
        if motivo and motivo not in self.motivos:
            self.motivos.append(motivo)


# --------------------------------------------------------------------------------------------------------------
# Rutas: el disco montado se ve con nombres exactos; Windows no distingue mayúsculas
# --------------------------------------------------------------------------------------------------------------
class Rutas:
    def __init__(self, raiz: str):
        self.raiz = os.path.abspath(raiz)
        self.real = os.path.realpath(raiz)
        self._dirs: dict[str, dict[str, str]] = {}

    def _listar(self, d: str) -> dict[str, str]:
        if d not in self._dirs:
            try:
                self._dirs[d] = {n.lower(): n for n in os.listdir(d)}
            except OSError:
                self._dirs[d] = {}
        return self._dirs[d]

    def buscar(self, *partes: str, base: str | None = None) -> str | None:
        """Ruta real de ``base/partes…`` sin distinguir mayúsculas (o None)."""
        p = base or self.raiz
        for parte in [x for s in partes for x in re.split(r"[\\/]+", s) if x and x != "."]:
            if parte == "..":
                return None          # nunca salir del disco analizado
            real = self._listar(p).get(parte.lower())
            if real is None:
                return None
            p = os.path.join(p, real)
        # Uniones y enlaces del disco analizado (hostil) nunca llevan fuera de él: ni a leer archivos de TISMA
        # ni, al limpiar, a escribirlos
        real = os.path.realpath(p)
        if real != self.real and not real.startswith(self.real + os.sep):
            return None
        return p


class Windows:
    """Una instalación de Windows en un disco montado."""

    def __init__(self, raiz: str):
        self.r = Rutas(raiz)
        self.raiz = self.r.raiz
        self.windir = None
        for nombre in ("Windows", "WINNT"):
            d = self.r.buscar(nombre, "System32", "config")
            if d and self.r.buscar("SOFTWARE", base=d):
                self.windir = self.r.buscar(nombre)
                break
        if not self.windir:
            raise FileNotFoundError(f"{raiz}: no parece un disco con Windows (falta Windows\\System32\\config)")
        self.config = self.r.buscar("System32", "config", base=self.windir)
        self.avisos: list[str] = []
        self.software = self._colmena("SOFTWARE")
        self.system = self._colmena("SYSTEM")
        cv = self.software.key(r"Microsoft\Windows NT\CurrentVersion") if self.software else None
        g = (lambda n, d="": (cv.get(n, d) if cv else d))
        self.producto = str(g("ProductName", "Windows"))
        self.version = str(g("CurrentVersion", ""))
        try:
            self.nt = tuple(int(x) for x in self.version.split(".")[:2])
        except ValueError:
            self.nt = (6, 1)
        self.sp = str(g("CSDVersion", ""))
        self.compilacion = str(g("CurrentBuildNumber", g("CurrentBuild", "")))
        sysroot = str(g("SystemRoot", "C:\\" + os.path.basename(self.windir)))
        self.unidad = sysroot[:2].upper() if re.match(r"[A-Za-z]:", sysroot) else "C:"
        self.x64 = bool(self.r.buscar("SysWOW64", base=self.windir))
        self.ccs = self._control_set()
        self.env = self._entorno(sysroot)
        self.usuarios = self._usuarios()

    # --- colmenas ---
    def _colmena(self, nombre: str, ruta: str | None = None) -> Hive | None:
        ruta = ruta or self.r.buscar(nombre, base=self.config)
        if not ruta:
            self.avisos.append(f"Falta la colmena {nombre}")
            return None
        try:
            h = Hive.open(ruta)
        except (OSError, RegfError) as e:
            self.avisos.append(f"No se pudo leer {ruta}: {e}")
            return None
        if h.sucia:
            self.avisos.append(f"{os.path.basename(ruta)}: Windows no se apagó del todo (o usa «inicio rápido»); "
                               "lo más reciente puede faltar en el análisis y no se limpiará el registro")
        return h

    def _control_set(self) -> str:
        sel = self.system.key("Select") if self.system else None
        n = sel.get("Current", 1) if sel else 1
        return f"ControlSet{int(n) if isinstance(n, int) else 1:03d}"

    def _entorno(self, sysroot: str) -> dict[str, str]:
        e = {"systemroot": sysroot, "windir": sysroot, "systemdrive": self.unidad}
        cv = self.software.key(r"Microsoft\Windows\CurrentVersion") if self.software else None
        if cv:
            for var, val in (("programfiles", "ProgramFilesDir"), ("programfiles(x86)", "ProgramFilesDir (x86)"),
                             ("commonprogramfiles", "CommonFilesDir"),
                             ("commonprogramfiles(x86)", "CommonFilesDir (x86)")):
                v = cv.get(val)
                if isinstance(v, str) and v:
                    e[var] = v
        e.setdefault("programfiles", self.unidad + "\\Program Files")
        e["programw6432"] = e["programfiles"]
        pl = self.software.key(r"Microsoft\Windows NT\CurrentVersion\ProfileList") if self.software else None
        if pl:
            for var, val in (("programdata", "ProgramData"), ("public", "Public")):
                v = pl.get(val)
                if isinstance(v, str) and v:
                    e[var] = v
            perfiles = pl.get("ProfilesDirectory")
            todos = pl.get("AllUsersProfile")
            if isinstance(perfiles, str) and isinstance(todos, str):          # XP: All Users
                e.setdefault("allusersprofile", f"{perfiles}\\{todos}")
        if "programdata" in e:
            e["allusersprofile"] = e["programdata"]
        if self.system:
            env = self.system.key(rf"{self.ccs}\Control\Session Manager\Environment")
            for v in env.values() if env else []:
                if isinstance(v.data, str) and v.name.lower() not in e:
                    e[v.name.lower()] = v.data
        return e

    def _usuarios(self) -> list[dict]:
        out = []
        pl = self.software.key(r"Microsoft\Windows NT\CurrentVersion\ProfileList") if self.software else None
        vistos = set()
        for k in pl.subkeys() if pl else []:
            ruta = k.get("ProfileImagePath")
            if not isinstance(ruta, str) or not k.name.startswith("S-1-5-21-"):
                continue          # cuentas de sistema (S-1-5-18/19/20): se revisan por la colmena DEFAULT/SYSTEM
            local = self.local(self.expandir(ruta))
            if local and local not in vistos:
                vistos.add(local)
                out.append({"nombre": os.path.basename(local), "sid": k.name, "perfil_win": self.expandir(ruta),
                            "perfil": local})
        if not out:                                    # ProfileList ilegible: buscar perfiles a mano
            for base in ("Users", "Documents and Settings"):
                d = self.r.buscar(base)
                for n in sorted(os.listdir(d)) if d else []:
                    p = os.path.join(d, n)
                    if self.r.buscar("NTUSER.DAT", base=p) and p not in vistos:
                        vistos.add(p)
                        out.append({"nombre": n, "sid": "", "perfil_win": f"{self.unidad}\\{base}\\{n}",
                                    "perfil": p})
        for u in out:
            nt = self.r.buscar("NTUSER.DAT", base=u["perfil"])
            u["hive"] = self._colmena("NTUSER.DAT", nt) if nt else None
            u["ntuser"] = nt
            u["env"] = self._entorno_usuario(u)
        return out

    def _entorno_usuario(self, u: dict) -> dict[str, str]:
        e = dict(self.env)
        e["userprofile"] = u["perfil_win"]
        e["username"] = u["nombre"]
        h = u.get("hive")
        sf = h.key(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") if h else None
        dato = (lambda n: sf.get(n) if sf else None)
        e["appdata"] = dato("AppData") or u["perfil_win"] + ("\\AppData\\Roaming" if self.nt >= (6, 0)
                                                            else "\\Application Data")
        e["localappdata"] = dato("Local AppData") or u["perfil_win"] + ("\\AppData\\Local" if self.nt >= (6, 0)
                                                                       else "\\Local Settings\\Application Data")
        env = h.key("Environment") if h else None
        for v in env.values() if env else []:
            if isinstance(v.data, str):
                e[v.name.lower()] = self.expandir(v.data, e)
        e.setdefault("temp", e["localappdata"] + "\\Temp")
        e.setdefault("tmp", e["temp"])
        return e

    # --- rutas de Windows a rutas locales ---
    def expandir(self, s: str, env: dict | None = None) -> str:
        env = env or self.env
        for _ in range(4):                                       # variables que contienen variables
            n = re.sub(r"%([^%\\]+)%", lambda m: env.get(m.group(1).lower(), m.group(0)), s)
            if n == s:
                break
            s = n
        return s

    def local(self, ruta_win: str, env: dict | None = None) -> str | None:
        """Ruta de este equipo para una ruta de Windows (None si está en otra unidad o no existe)."""
        p = self.expandir(ruta_win.strip().strip('"'), env).replace("/", "\\")
        p = re.sub(r"^(\\\?\?\\|\\\\\?\\)", "", p)
        if p.lower().startswith("\\systemroot\\"):
            p = self.env["systemroot"] + p[11:]
        elif re.match(r"(?i)^system32\\", p):                      # ImagePath de drivers: relativo a Windows
            p = self.env["systemroot"] + "\\" + p
        if re.match(r"[A-Za-z]:", p):
            if p[:2].upper() != self.unidad:
                return None
            return self.r.buscar(p[2:])
        if "\\" not in p:                                        # nombre suelto: se busca como lo haría Windows
            nombres = [p] if os.path.splitext(p)[1] else [p + ".exe", p]
            for d in ("System32", "", "SysWOW64", "System32\\wbem", "System32\\WindowsPowerShell\\v1.0"):
                for n in nombres:
                    r = self.r.buscar(d, n, base=self.windir)
                    if r and os.path.isfile(r):
                        return r
        return None

    def a_windows(self, local: str) -> str:
        rel = os.path.relpath(local, self.raiz)
        return self.unidad + "\\" + ("" if rel == "." else rel.replace("/", "\\"))

    def es_del_sistema(self, local: str) -> bool:
        d = os.path.dirname(local).lower()
        w = self.windir.lower()
        return d in (w, os.path.join(w, "system32"), os.path.join(w, "syswow64")) or \
            d.startswith(os.path.join(w, "system32").lower() + os.sep) or \
            d.startswith(os.path.join(w, "syswow64").lower() + os.sep) or \
            d.startswith(os.path.join(w, "winsxs").lower() + os.sep)


# --------------------------------------------------------------------------------------------------------------
# Órdenes: qué programa (y qué script o DLL) ejecuta una línea de órdenes
# --------------------------------------------------------------------------------------------------------------
def partir(cmd: str) -> list[str]:
    """Separa una línea de órdenes de Windows en palabras (comillas dobles, sin escapes)."""
    out, cur, comillas = [], "", False
    for ch in cmd:
        if ch == '"':
            comillas = not comillas
        elif ch in " \t" and not comillas:
            if cur:
                out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def programa(cmd: str, w: Windows | None = None, env: dict | None = None) -> str:
    """El ejecutable (ruta de Windows) de una línea de órdenes, también sin comillas y con espacios
    («C:\\Program Files\\X\\x.exe -a»)."""
    cmd = cmd.strip()
    if cmd.startswith('"'):
        return cmd[1:].split('"', 1)[0]
    pal = cmd.split(" ")
    candidato = None
    for i in range(1, len(pal) + 1):
        p = " ".join(pal[:i]).rstrip(",")
        if w is not None and (lo := w.local(p, env)) and os.path.isfile(lo):
            return p
        if candidato is None and p.lower().endswith(EJECUTABLES):
            candidato = p
    return candidato or pal[0].rstrip(",")


def objetivos(cmd: str, w: Windows | None = None, env: dict | None = None) -> list[str]:
    """Ejecutable + script/DLL que carga (rundll32 x.dll,Entrada; wscript x.vbs; regsvr32 x.dll; cmd /c x.bat)."""
    exe = programa(cmd, w, env)
    out = [exe]
    resto = cmd.strip()
    resto = resto[len(exe) + 2:] if resto.startswith('"') else resto[len(exe):]
    base = os.path.basename(exe.replace("\\", "/")).lower()
    if base.split(".")[0] in ("rundll32", "regsvr32", "wscript", "cscript", "mshta", "cmd", "javaw", "java",
                               "msiexec", "powershell", "pwsh", "start", "explorer"):
        for p in partir(resto):
            p = re.sub(r"^/[a-z]+:", "", p, flags=re.I).split(",")[0]
            if p.lower().endswith(EJECUTABLES) and not p.startswith(("/", "-")):
                out.append(p)
    return out


def evaluar_comando(h: Hallazgo, cmd: str) -> None:
    for rx, grav, motivo in LOLBIN:
        if rx.search(cmd):
            h.subir(grav, motivo)
            if grav == "high":
                break


def pe_firmado(ruta: str) -> bool | None:
    """¿Tiene el ejecutable una firma Authenticode? (presencia de la tabla de certificados; no la valida).
    None si no es un PE."""
    try:
        with open(ruta, "rb") as fh:
            dos = fh.read(64)
            if len(dos) < 64 or dos[:2] != b"MZ":
                return None
            pe = struct.unpack_from("<I", dos, 60)[0]
            if pe > 1 << 20:
                return None
            fh.seek(pe)
            cab = fh.read(24 + 240)
    except OSError:
        return None
    if len(cab) < 24 + 2 or cab[:4] != b"PE\0\0":
        return None
    magic = struct.unpack_from("<H", cab, 24)[0]
    dirs = {0x10B: 24 + 96, 0x20B: 24 + 112}.get(magic)
    if dirs is None or len(cab) < dirs + 8 * 5:
        return None
    off, tam = struct.unpack_from("<II", cab, dirs + 8 * 4)       # entrada 4: IMAGE_DIRECTORY_ENTRY_SECURITY
    return bool(off and tam)


# --------------------------------------------------------------------------------------------------------------
# Accesos directos (.lnk) y tareas de XP (.job)
# --------------------------------------------------------------------------------------------------------------
def leer_lnk(ruta: str) -> str:
    """Destino + argumentos de un acceso directo (MS-SHLLINK), o ''."""
    try:
        with open(ruta, "rb") as fh:
            d = fh.read(1 << 20)
    except OSError:
        return ""
    if len(d) < 0x4C or d[:4] != b"\x4c\0\0\0":
        return ""
    flags = struct.unpack_from("<I", d, 0x14)[0]
    pos = 0x4C
    try:
        if flags & 0x1:                                  # HasLinkTargetIDList
            pos += 2 + struct.unpack_from("<H", d, pos)[0]
        destino = ""
        if flags & 0x2:                                  # HasLinkInfo
            tam, cab, _f, _vol, base = struct.unpack_from("<IIIII", d, pos)
            if cab >= 0x24 and pos + 0x1C + 4 <= len(d):
                ubase = struct.unpack_from("<I", d, pos + 0x1C)[0]
                if ubase:
                    destino = d[pos + ubase:pos + tam].decode("utf-16-le", "replace").split("\0")[0]
            if not destino and base:
                destino = d[pos + base:pos + tam].split(b"\0")[0].decode("mbcs" if os.name == "nt" else "cp1252",
                                                                         "replace")
            pos += tam
        uni = flags & 0x80
        args = ""
        for bit in (0x4, 0x8, 0x10, 0x20):               # nombre, ruta relativa, carpeta, argumentos
            if flags & bit:
                n = struct.unpack_from("<H", d, pos)[0]
                pos += 2
                largo = n * 2 if uni else n
                s = d[pos:pos + largo].decode("utf-16-le" if uni else "cp1252", "replace")
                pos += largo
                if bit == 0x8 and not destino:
                    destino = s
                if bit == 0x20:
                    args = s
    except struct.error:
        return ""
    if not destino:
        return ""
    return (f'"{destino}" {args}' if " " in destino else f"{destino} {args}").strip()


def leer_job(ruta: str) -> str:
    """Programa + parámetros de una tarea de XP (.job, MS-TSCH «fixed-length section» + nombre)."""
    try:
        with open(ruta, "rb") as fh:
            d = fh.read(1 << 16)
        pos = 0x44 + 2                                   # sección fija + contador de instancias
        n = struct.unpack_from("<H", d, pos)[0]
        app = d[pos + 2:pos + 2 + 2 * n].decode("utf-16-le", "replace").rstrip("\0")
        pos += 2 + 2 * n
        n = struct.unpack_from("<H", d, pos)[0]
        par = d[pos + 2:pos + 2 + 2 * n].decode("utf-16-le", "replace").rstrip("\0")
    except (OSError, struct.error):
        return ""
    return (f'"{app}" {par}' if " " in app else f"{app} {par}").strip()


# --------------------------------------------------------------------------------------------------------------
# Análisis
# --------------------------------------------------------------------------------------------------------------
class Analisis:
    def __init__(self, w: Windows, scanner=None, log=None):
        self.w = w
        self.scanner = scanner
        self.h: list[Hallazgo] = []
        self._escaneados: dict[str, list] = {}
        self.log = log or (lambda m: None)

    # utilidades
    def _escanear(self, ruta: str) -> list:
        if self.scanner is None or not ruta:
            return []
        if ruta not in self._escaneados:
            try:
                self._escaneados[ruta] = self.scanner.scan_file(ruta)
            except OSError:
                self._escaneados[ruta] = []
        return self._escaneados[ruta]

    def entrada(self, tipo: str, donde: str, cmd: str, env: dict | None = None, base: str = "info",
                arreglo: dict | None = None, usuario: str = "", motivo: str = "") -> Hallazgo:
        """Un punto de inicio automático: localiza lo que ejecuta, lo evalúa y lo escanea."""
        h = Hallazgo(tipo, base, donde, cmd, arreglo=arreglo, usuario=usuario)
        if motivo:
            h.motivos.append(motivo)
        evaluar_comando(h, cmd)
        # Winlogon admite listas separadas por comas («explorer.exe, C:\\ProgramData\\x.exe»)
        partes = [p for p in cmd.split(",") if p.strip()] if tipo == "winlogon" else [cmd]
        objs = [o for p in partes for o in objetivos(p, self.w, env)]
        for i, obj in enumerate(objs):
            win = self.w.expandir(obj, env)
            loc = self.w.local(win, env)
            nombre = os.path.basename(win.replace("\\", "/")).lower()
            if i == 0:
                h.archivo = loc or ""
            marcar = False                    # ¿este archivo concreto es sospechoso? (→ cuarentena al limpiar)
            if DOBLE_EXT.search(nombre):
                h.subir("high", f"doble extensión ({nombre})")
                marcar = True
            if loc is None:
                if re.match(r"[A-Za-z]:", win) and win[:2].upper() != self.w.unidad:
                    h.subir("low", f"apunta a otra unidad ({win[:2]})")
                elif not CMD_INFORMATIVO.match(cmd.strip()):
                    h.subir("low", f"el archivo no existe ({win})")
                continue
            if nombre in SISTEMA and not self.w.es_del_sistema(loc):
                h.subir("high", f"se hace pasar por un programa de Windows ({nombre}) fuera de su carpeta")
                marcar = True
            if ESCRIBIBLE.search(win.replace("/", "\\").rsplit("\\", 1)[0] + "\\"):
                marcar = True
                if nombre.endswith((".vbs", ".vbe", ".js", ".jse", ".wsf", ".hta", ".ps1", ".bat", ".cmd")):
                    h.subir("high", "script en una carpeta de usuario o temporal")
                elif pe_firmado(loc) is False:
                    h.subir("medium", "programa sin firma en una carpeta de usuario o temporal")
                else:
                    h.subir("low", "programa en una carpeta de usuario")
            for d in self._escanear(loc):
                h.detecciones.append(f"{d.name} ({d.method})")
                h.subir(d.severity if d.method != "ml" else "low", "detectado por el antivirus")
                marcar = True
            if marcar and not self.w.es_del_sistema(loc) and loc not in h.sospechosos:
                h.sospechosos.append(loc)
        self.h.append(h)
        return h

    def debilidad(self, tipo: str, gravedad: str, donde: str, motivo: str, arreglo: dict | None = None):
        self.h.append(Hallazgo(tipo, gravedad, donde, motivos=[motivo], arreglo=arreglo))

    # --- comprobaciones ---
    def ejecutar(self, completo: bool = False, sin_archivos: bool = False) -> list[Hallazgo]:
        pasos = [("claves Run de la máquina", self.run_maquina), ("usuarios", self.run_usuarios),
                 ("Winlogon", self.winlogon), ("Image File Execution Options", self.ifeo),
                 ("AppInit/AppCert/LSA/BootExecute", self.cargadores), ("servicios y drivers", self.servicios),
                 ("extensiones de Explorer e Internet Explorer", self.extensiones),
                 ("tareas programadas", self.tareas), ("carpetas de Inicio", self.carpetas_inicio),
                 ("archivo hosts", self.hosts), ("configuración de seguridad", self.configuracion)]
        if not sin_archivos:
            pasos.append(("archivos", lambda: self.archivos(completo)))
        for nombre, f in pasos:
            self.log(f"Revisando {nombre}…")
            try:
                f()
            except RegfError as e:
                self.w.avisos.append(f"{nombre}: registro dañado ({e})")
        return self.h

    def _run(self, hive: Hive | None, raiz: str, colmena: str, env=None, usuario=""):
        if hive is None:
            return
        rutas = [r"Microsoft\Windows\CurrentVersion\Run", r"Microsoft\Windows\CurrentVersion\RunOnce",
                 r"Microsoft\Windows\CurrentVersion\RunOnceEx", r"Microsoft\Windows\CurrentVersion\RunServices",
                 r"Microsoft\Windows\CurrentVersion\RunServicesOnce",
                 r"Microsoft\Windows\CurrentVersion\Policies\Explorer\Run"]
        pref = [raiz, raiz + r"\Wow6432Node"] if raiz else ["", "Wow6432Node"]
        for p in pref:
            for r in rutas:
                clave = f"{p}\\{r}".strip("\\")
                k = hive.key(clave)
                for v in k.values() if k else []:
                    if isinstance(v.data, str) and v.data.strip():
                        self.entrada("inicio", f"{colmena}\\{clave} «{v.name}»", v.data, env, usuario=usuario,
                                     arreglo={"colmena": hive.nombre, "clave": clave, "valor": v.name,
                                              "accion": "borrar"})

    def run_maquina(self):
        self._run(self.w.software, "", "HKLM\\SOFTWARE")

    def run_usuarios(self):
        for u in self.w.usuarios:
            h = u.get("hive")
            if h is None:
                continue
            col = f"HKCU({u['nombre']})"
            self._run(h, "Software", col, u["env"], u["nombre"])
            win = h.key(r"Software\Microsoft\Windows NT\CurrentVersion\Windows")
            for n in ("Load", "Run"):
                v = win.get(n) if win else None
                if isinstance(v, str) and v.strip():
                    self.entrada("inicio", f"{col}\\…\\Windows NT\\CurrentVersion\\Windows «{n}»", v, u["env"],
                                 base="medium", usuario=u["nombre"], motivo="método antiguo de inicio, hoy casi "
                                                                            "solo lo usa el malware",
                                 arreglo={"colmena": h.nombre, "clave": win.path, "valor": n, "accion": "borrar"})
            wl = h.key(r"Software\Microsoft\Windows NT\CurrentVersion\Winlogon")
            v = wl.get("Shell") if wl else None
            if isinstance(v, str) and v.strip():
                self.entrada("winlogon", f"{col}\\…\\Winlogon «Shell»", v, u["env"], base="high",
                             usuario=u["nombre"], motivo="el usuario sustituye el escritorio de Windows",
                             arreglo={"colmena": h.nombre, "clave": wl.path, "valor": "Shell", "accion": "borrar"})
            pol = h.key(r"Software\Microsoft\Windows\CurrentVersion\Policies\System")
            if pol and pol.get("DisableTaskMgr") == 1:
                self.debilidad("configuracion", "medium", f"{col}\\…\\Policies\\System «DisableTaskMgr»",
                               "el Administrador de tareas está bloqueado (típico del malware)",
                               {"colmena": h.nombre, "clave": pol.path, "valor": "DisableTaskMgr",
                                "accion": "dword", "dato": 0})
            if pol and pol.get("DisableRegistryTools") in (1, 2):
                self.debilidad("configuracion", "medium", f"{col}\\…\\Policies\\System «DisableRegistryTools»",
                               "el Editor del registro está bloqueado (típico del malware)",
                               {"colmena": h.nombre, "clave": pol.path, "valor": "DisableRegistryTools",
                                "accion": "dword", "dato": 0})

    def winlogon(self):
        s = self.w.software
        wl = s.key(r"Microsoft\Windows NT\CurrentVersion\Winlogon") if s else None
        if not wl:
            return
        sysroot = self.w.env["systemroot"]
        shell = wl.get("Shell")
        if isinstance(shell, str) and shell.strip().lower() not in ("explorer.exe", "explorer"):
            h = self.entrada("winlogon", r"HKLM\…\Winlogon «Shell»", shell, base="high",
                             motivo="debería ser solo «explorer.exe»",
                             arreglo={"colmena": s.nombre, "clave": wl.path, "valor": "Shell", "accion": "cadena",
                                      "dato": "explorer.exe"})
            h.comando = shell
        ui = wl.get("Userinit")
        if isinstance(ui, str):
            partes = [p.strip() for p in ui.split(",") if p.strip()]
            normal = all(re.fullmatch(r'(?i)"?([a-z]:\\[^,]*\\system32\\)?userinit(\.exe)?"?', p) for p in partes)
            if not normal:
                self.entrada("winlogon", r"HKLM\…\Winlogon «Userinit»", ui, base="high",
                             motivo="debería ser solo userinit.exe",
                             arreglo={"colmena": s.nombre, "clave": wl.path, "valor": "Userinit",
                                      "accion": "cadena", "dato": sysroot + "\\system32\\userinit.exe,"})
        for n in ("Taskman", "AppSetup", "VmApplet"):
            v = wl.get(n)
            if isinstance(v, str) and v.strip() and not (n == "VmApplet" and "sysdm.cpl" in v.lower()):
                self.entrada("winlogon", rf"HKLM\…\Winlogon «{n}»", v, base="medium",
                             motivo="programa extra lanzado por Winlogon",
                             arreglo={"colmena": s.nombre, "clave": wl.path, "valor": n, "accion": "borrar"})
        gina = wl.get("GinaDLL")
        if isinstance(gina, str) and gina.strip() and gina.strip().lower() != "msgina.dll":
            self.entrada("winlogon", r"HKLM\…\Winlogon «GinaDLL»", gina, base="medium",
                         motivo="pantalla de inicio de sesión sustituida (XP): puede capturar contraseñas")
        notify = wl.subkey("Notify")
        normales = {"crypt32chain", "cryptnet", "cscdll", "dimsntfy", "scecli", "sclgntfy", "senslogn", "termsrv",
                    "wlballoon", "wgalogon", "igfxcui", "ati2evxx", "hotkeycmds", "wzcnotif", "rdpclip"}
        for k in notify.subkeys() if notify else []:
            dll = k.get("DLLName")
            if isinstance(dll, str):
                self.entrada("winlogon", rf"HKLM\…\Winlogon\Notify\{k.name}", dll,
                             base="info" if k.name.lower() in normales else "low",
                             motivo="" if k.name.lower() in normales else "DLL cargada por Winlogon (XP)")

    def ifeo(self):
        s = self.w.software
        for raiz in ("", "Wow6432Node\\"):
            k = s.key(raiz + r"Microsoft\Windows NT\CurrentVersion\Image File Execution Options") if s else None
            for sub in k.subkeys() if k else []:
                dbg = sub.get("Debugger")
                if isinstance(dbg, str) and dbg.strip():
                    acc = sub.name.lower() in ACCESIBILIDAD
                    shell = re.search(r"(?i)\b(cmd|powershell|explorer|taskmgr)(\.exe)?\b", dbg)
                    grav = "high" if acc or shell else "medium"
                    mot = ("puerta trasera: abre este programa desde la pantalla de inicio de sesión, sin contraseña"
                           if acc else f"al abrir {sub.name} se ejecuta otra cosa")
                    if sub.name.lower() == "taskmgr.exe" and re.search(r"(?i)procexp", dbg):
                        grav, mot = "low", "Process Explorer sustituye al Administrador de tareas"
                    self.entrada("ifeo", f"HKLM\\…\\Image File Execution Options\\{sub.name} «Debugger»", dbg,
                                 base=grav, motivo=mot,
                                 arreglo={"colmena": s.nombre, "clave": sub.path, "valor": "Debugger",
                                          "accion": "borrar"})
                gf = sub.get("GlobalFlag")
                if isinstance(gf, int) and gf & 0x200:
                    spe = s.key(raiz + rf"Microsoft\Windows NT\CurrentVersion\SilentProcessExit\{sub.name}")
                    mon = spe.get("MonitorProcess") if spe else None
                    if isinstance(mon, str) and mon.strip():
                        self.entrada("ifeo", f"HKLM\\…\\SilentProcessExit\\{sub.name} «MonitorProcess»", mon,
                                     base="high", motivo=f"se ejecuta cada vez que {sub.name} termina",
                                     arreglo={"colmena": s.nombre, "clave": spe.path, "valor": "MonitorProcess",
                                              "accion": "borrar"})

    def cargadores(self):
        s, y = self.w.software, self.w.system
        for raiz in ("", "Wow6432Node\\"):
            k = s.key(raiz + r"Microsoft\Windows NT\CurrentVersion\Windows") if s else None
            dlls = k.get("AppInit_DLLs") if k else None
            if isinstance(dlls, str) and dlls.strip():
                activo = k.get("LoadAppInit_DLLs", 1 if self.w.nt < (6, 1) else 0)
                for dll in re.split(r"[ ,]+", dlls.strip()):
                    self.entrada("appinit", f"HKLM\\…\\{raiz}Windows NT\\CurrentVersion\\Windows «AppInit_DLLs»",
                                 dll, base="high" if activo else "low",
                                 motivo="DLL que se carga en todos los programas con ventana" +
                                        ("" if activo else " (desactivado por LoadAppInit_DLLs=0)"),
                                 arreglo={"colmena": s.nombre, "clave": k.path, "valor": "AppInit_DLLs",
                                          "accion": "cadena", "dato": ""})
        if not y:
            return
        sm = y.key(rf"{self.w.ccs}\Control\Session Manager")
        if sm:
            be = sm.get("BootExecute", [])
            for e in be if isinstance(be, list) else [be]:
                if e.strip().lower() not in ("autocheck autochk *", "autocheck autochk /q /v *"):
                    self.entrada("arranque", r"HKLM\SYSTEM\…\Session Manager «BootExecute»", e, base="medium",
                                 motivo="programa ejecutado antes de que arranque Windows (algunos antivirus "
                                        "y desfragmentadores lo usan)")
            ac = sm.subkey("AppCertDlls")
            for v in ac.values() if ac else []:
                if isinstance(v.data, str):
                    self.entrada("appcert", rf"HKLM\SYSTEM\…\AppCertDlls «{v.name}»", v.data, base="high",
                                 motivo="DLL inyectada en todo programa que cree procesos",
                                 arreglo={"colmena": y.nombre, "clave": ac.path, "valor": v.name,
                                          "accion": "borrar"})
        lsa = y.key(rf"{self.w.ccs}\Control\Lsa")
        normales = {"msv1_0", "scecli", "kerberos", "schannel", "wdigest", "tspkg", "pku2u", "livessp", "cloudap",
                    "negoexts", "rassfm", "kdcsvc", "wsauth", ""}
        for n in ("Authentication Packages", "Notification Packages", "Security Packages"):
            v = lsa.get(n) if lsa else None
            for p in (v if isinstance(v, list) else [v] if isinstance(v, str) else []):
                p = p.strip().strip('"')
                if p.lower().removesuffix(".dll") not in normales:
                    dll = p if os.path.splitext(p)[1] else p + ".dll"       # nombres sin extensión, en System32
                    self.entrada("lsa", rf"HKLM\SYSTEM\…\Control\Lsa «{n}»", dll, base="medium",
                                 motivo="DLL cargada por LSA: ve las contraseñas al iniciar sesión o cambiarlas")

    def servicios(self):
        y = self.w.system
        k = y.key(rf"{self.w.ccs}\Services") if y else None
        for sv in k.subkeys() if k else []:
            img = sv.get("ImagePath")
            inicio = sv.get("Start", 3)
            tipo = sv.get("Type", 0)
            if not isinstance(img, str) or not img.strip():
                continue
            auto = isinstance(inicio, int) and inicio <= 2
            h = self.entrada("servicio", f"HKLM\\SYSTEM\\…\\Services\\{sv.name}", img, base="info",
                             arreglo={"colmena": y.nombre, "clave": sv.path, "valor": "Start", "accion": "dword",
                                      "dato": 4})
            if not auto and h.gravedad == "low":
                h.gravedad = "info"           # servicio manual o desactivado que apunta a algo que no existe
            dll_k = sv.subkey("Parameters")
            dll = dll_k.get("ServiceDll") if dll_k else None
            if isinstance(dll, str) and dll.strip():
                h2 = self.entrada("servicio", f"HKLM\\SYSTEM\\…\\Services\\{sv.name}\\Parameters «ServiceDll»", dll,
                                  base="info", arreglo=h.arreglo)
                if h2.archivo and not self.w.es_del_sistema(h2.archivo) and "svchost" in img.lower():
                    h2.subir("medium", "DLL de servicio de svchost fuera de System32")
            if isinstance(tipo, int) and tipo & 0x3 and h.archivo and not self.w.es_del_sistema(h.archivo):
                h.subir("low" if auto else "info", "driver fuera de la carpeta de drivers de Windows")

    def extensiones(self):
        s = self.w.software
        if not s:
            return

        def clsid(c: str) -> str | None:
            for raiz in ("Classes", "Wow6432Node\\Classes"):
                k = s.key(rf"{raiz}\CLSID\{c}\InprocServer32")
                v = k.get("") if k else None
                if isinstance(v, str) and v.strip():
                    return v
            return None

        for raiz in ("", "Wow6432Node\\"):
            bho = s.key(raiz + r"Microsoft\Windows\CurrentVersion\Explorer\Browser Helper Objects")
            for k in bho.subkeys() if bho else []:
                dll = clsid(k.name)
                if dll:
                    self.entrada("extension", rf"HKLM\…\Browser Helper Objects\{k.name}", dll, base="info",
                                 motivo="extensión de Internet Explorer")
            sso = s.key(raiz + r"Microsoft\Windows\CurrentVersion\ShellServiceObjectDelayLoad")
            for v in sso.values() if sso else []:
                dll = clsid(v.data) if isinstance(v.data, str) else None
                if dll:
                    self.entrada("extension", rf"HKLM\…\ShellServiceObjectDelayLoad «{v.name}»", dll, base="info")

    def tareas(self):
        d = self.w.r.buscar("System32", "Tasks", base=self.w.windir)
        for dirpath, _dirs, files in os.walk(d) if d else []:
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    if os.path.getsize(p) > 1 << 20:
                        continue
                    datos = open(p, "rb").read()
                    raiz = ET.fromstring(datos)
                except (OSError, ET.ParseError, ValueError):
                    continue
                ns = {"t": raiz.tag[1:].split("}")[0]} if raiz.tag.startswith("{") else {}
                pre = "t:" if ns else ""
                oculta = (raiz.findtext(f".//{pre}Settings/{pre}Hidden", "", ns) or "").lower() == "true"
                for ex in raiz.findall(f".//{pre}Actions/{pre}Exec", ns):
                    cmd = (ex.findtext(f"{pre}Command", "", ns) or "").strip()
                    args = (ex.findtext(f"{pre}Arguments", "", ns) or "").strip()
                    if not cmd:
                        continue
                    linea = (f'"{cmd}"' if " " in cmd and not cmd.startswith('"') else cmd) + (f" {args}" if args
                                                                                                else "")
                    h = self.entrada("tarea", "Tarea " + self.w.a_windows(p), linea, base="info",
                                     arreglo={"archivo": p})
                    if oculta and h.gravedad != "info":
                        h.subir(h.gravedad, "tarea oculta")
        d = self.w.r.buscar("Tasks", base=self.w.windir)                 # XP
        for f in sorted(os.listdir(d)) if d else []:
            if f.lower().endswith(".job"):
                p = os.path.join(d, f)
                cmd = leer_job(p)
                if cmd:
                    self.entrada("tarea", "Tarea " + self.w.a_windows(p), cmd, base="info", arreglo={"archivo": p})

    def carpetas_inicio(self):
        carpetas = []
        s = self.w.software
        sf = s.key(r"Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") if s else None
        v = sf.get("Common Startup") if sf else None
        carpetas.append((v if isinstance(v, str) else
                         self.w.env.get("programdata", self.w.env.get("allusersprofile", "")) +
                         r"\Microsoft\Windows\Start Menu\Programs\Startup", None, ""))
        for u in self.w.usuarios:
            h = u.get("hive")
            sfu = h.key(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") if h else None
            v = sfu.get("Startup") if sfu else None
            carpetas.append((v if isinstance(v, str) else
                             u["env"]["appdata"] + r"\Microsoft\Windows\Start Menu\Programs\Startup", u["env"],
                             u["nombre"]))
        vistas = set()
        for win, env, usuario in carpetas:
            d = self.w.local(win, env)
            if not d or d in vistas or not os.path.isdir(d):
                continue
            vistas.add(d)
            for f in sorted(os.listdir(d)):
                p = os.path.join(d, f)
                if f.lower() == "desktop.ini" or not os.path.isfile(p):
                    continue
                donde = "Carpeta de Inicio " + self.w.a_windows(p)
                if f.lower().endswith(".lnk"):
                    cmd = leer_lnk(p)
                    if cmd:
                        self.entrada("carpeta_inicio", donde, cmd, env, usuario=usuario, arreglo={"archivo": p})
                    continue
                h = self.entrada("carpeta_inicio", donde, self.w.a_windows(p), env, usuario=usuario,
                                 arreglo={"archivo": p})
                if f.lower().endswith((".exe", ".scr", ".com", ".pif", ".vbs", ".vbe", ".js", ".jse", ".wsf",
                                       ".hta", ".bat", ".cmd", ".ps1")):
                    h.subir("medium", "programa o script directamente en la carpeta de Inicio")

    def hosts(self):
        p = self.w.r.buscar("System32", "drivers", "etc", "hosts", base=self.w.windir)
        if not p:
            return
        try:
            lineas = open(p, "rb").read(1 << 20).decode("latin-1").splitlines()
        except OSError:
            return
        sensibles = re.compile(r"(microsoft|windowsupdate|windows\.com|msftncsi|live\.com|google|bing|facebook|"
                               r"paypal|bank|banco|santander|bbva|caixa|itau|bradesco|mercadopago|avast|avg|eset|"
                               r"kaspersky|mcafee|norton|symantec|malwarebytes|bitdefender|sophos|virustotal|"
                               r"trendmicro|avira|clamav|0patch)", re.I)
        malas = []
        for i, l in enumerate(lineas):
            l = l.split("#", 1)[0].strip()
            if not l:
                continue
            ip, *nombres = l.split()
            for n in nombres:
                if n.lower() in ("localhost", "localhost.localdomain") or not sensibles.search(n):
                    continue
                bloquea = ip in ("127.0.0.1", "0.0.0.0", "::1")
                malas.append((i, n, ip, bloquea))
        if malas:
            redir = [m for m in malas if not m[3]]
            bloq = [m for m in malas if m[3]]
            if redir:
                self.debilidad("hosts", "high", self.w.a_windows(p),
                               "redirige sitios importantes a otro servidor (robo de contraseñas): " +
                               ", ".join(f"{n} → {ip}" for _, n, ip, _ in redir[:6]), {"hosts": p})
            if bloq:
                self.debilidad("hosts", "medium", self.w.a_windows(p),
                               "bloquea sitios de seguridad o de actualizaciones: " +
                               ", ".join(n for _, n, _, _ in bloq[:8]), {"hosts": p})

    def configuracion(self):
        s, y = self.w.software, self.w.system
        pol = s.key(r"Microsoft\Windows\CurrentVersion\Policies\System") if s else None
        if pol and pol.get("EnableLUA") == 0 and self.w.nt >= (6, 0):
            self.debilidad("configuracion", "medium", r"HKLM\…\Policies\System «EnableLUA»",
                           "el Control de cuentas de usuario (UAC) está desactivado",
                           {"colmena": s.nombre, "clave": pol.path, "valor": "EnableLUA", "accion": "dword",
                            "dato": 1})
        for clave in (r"Policies\Microsoft\Windows Defender", r"Microsoft\Windows Defender"):
            k = s.key(clave) if s else None
            if k and k.get("DisableAntiSpyware") == 1:
                self.debilidad("configuracion", "medium", rf"HKLM\SOFTWARE\{clave} «DisableAntiSpyware»",
                               "Windows Defender está desactivado",
                               {"colmena": s.nombre, "clave": k.path, "valor": "DisableAntiSpyware",
                                "accion": "dword", "dato": 0})
        if not y:
            return
        for perfil in ("StandardProfile", "DomainProfile", "PublicProfile"):
            k = y.key(rf"{self.w.ccs}\Services\SharedAccess\Parameters\FirewallPolicy\{perfil}")
            if k and k.get("EnableFirewall") == 0:
                self.debilidad("configuracion", "medium", rf"HKLM\SYSTEM\…\FirewallPolicy\{perfil}",
                               "el cortafuegos de Windows está desactivado",
                               {"colmena": y.nombre, "clave": k.path, "valor": "EnableFirewall", "accion": "dword",
                                "dato": 1})
        for svc, nombre in (("wuauserv", "Windows Update"), ("WinDefend", "Windows Defender"),
                            ("MpsSvc", "el cortafuegos"), ("SharedAccess", "el cortafuegos (XP)"),
                            ("wscsvc", "el Centro de seguridad")):
            k = y.key(rf"{self.w.ccs}\Services\{svc}")
            if k and k.get("Start") == 4 and not (svc == "SharedAccess" and self.w.nt >= (6, 0)):
                self.debilidad("configuracion", "medium", rf"HKLM\SYSTEM\…\Services\{svc} «Start»",
                               f"el servicio de {nombre} está desactivado",
                               {"colmena": y.nombre, "clave": k.path, "valor": "Start", "accion": "dword",
                                "dato": 2})
        smb = y.key(rf"{self.w.ccs}\Services\LanmanServer\Parameters")
        if smb and smb.get("SMB1", 1) != 0 and self.w.nt >= (6, 0):
            self.debilidad("configuracion", "low", r"HKLM\SYSTEM\…\LanmanServer\Parameters «SMB1»",
                           "SMBv1 activado (la puerta de WannaCry); el kit de endurecimiento lo apaga")

    def archivos(self, completo: bool):
        """Escaneo de archivos: el disco entero o los lugares donde suele caer el malware."""
        if self.scanner is None:
            return
        if completo:
            raices = [self.w.raiz]
        else:
            raices = [self.w.local(r"%systemroot%\Temp")]
            pd = self.w.env.get("programdata")
            if pd:
                raices.append(self.w.local(pd))
            for u in self.w.usuarios:
                e = u["env"]
                h = u.get("hive")
                sf = h.key(r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders") if h else None
                for n, defecto in (("Desktop", "%userprofile%\\Desktop"),
                                   ("{374DE290-123F-4565-9164-39C4925E467B}", "%userprofile%\\Downloads"),
                                   ("AppData", "%appdata%"), ("Local AppData", "%localappdata%")):
                    v = sf.get(n) if sf else None
                    raices.append(self.w.local(v if isinstance(v, str) else defecto, e))
                raices.append(self.w.local("%temp%", e))
        vistos = set()
        poco_profundos = {self.w.local(u["env"]["appdata"], u["env"]) for u in self.w.usuarios} | \
                         {self.w.local(u["env"]["localappdata"], u["env"]) for u in self.w.usuarios}
        if self.w.env.get("programdata"):
            poco_profundos.add(self.w.local(self.w.env["programdata"]))
        for r in [x for x in raices if x and os.path.isdir(x)]:
            prof_max = 2 if r in poco_profundos and not completo else 64
            base = r.rstrip(os.sep).count(os.sep)
            for dirpath, dirs, files in os.walk(r):
                if dirpath.count(os.sep) - base >= prof_max:
                    dirs[:] = []
                if not completo:
                    # Cachés de navegadores: miles de archivos y nada ejecutable
                    dirs[:] = [d for d in dirs if d.lower() not in ("cache", "cache2", "code cache", "gpucache",
                                                                     "temporary internet files", "inetcache",
                                                                     "webcache", "service worker")]
                for f in files:
                    p = os.path.join(dirpath, f)
                    if p in vistos or p in self._escaneados or os.path.islink(p):
                        continue
                    vistos.add(p)
                    dets = [d for d in self._escanear(p) if d.severity != "low"]
                    if dets:
                        peor = max(dets, key=lambda d: ORDEN.get(d.severity, 0))
                        self.h.append(Hallazgo("archivo", peor.severity, self.w.a_windows(p),
                                               archivo=p, motivos=["detectado por el antivirus"],
                                               detecciones=[f"{d.name} ({d.method})" for d in dets],
                                               arreglo={"archivo": p}))


# --------------------------------------------------------------------------------------------------------------
# Limpieza
# --------------------------------------------------------------------------------------------------------------
def limpiar(w: Windows, hallazgos: list[Hallazgo], niveles=("high",), quarantine=None, log=print) -> dict:
    """Desactiva lo encontrado: archivos a la cuarentena del propio disco, valores del registro con cambios
    mínimos (copia previa de cada colmena) y el archivo hosts sin las líneas malas (copia previa).
    Nunca aparta archivos de Windows (System32, SysWOW64, WinSxS): para esos se corrige la entrada que los usa."""
    from .quarantine import Quarantine
    dest = os.path.join(w.raiz, "TISMA-Cuarentena")
    q = quarantine or Quarantine(Path(dest))
    copias = os.path.join(dest, "registro", time.strftime("%Y%m%d-%H%M%S"))
    hecho = {"archivos": [], "registro": [], "errores": [], "copias": copias}
    editores: dict[str, Editor] = {}
    elegidos = [h for h in hallazgos if h.gravedad in niveles]

    def apartar(ruta: str, motivo: str):
        tareas = os.path.join(w.windir, "System32", "Tasks").lower() + os.sep
        if not ruta or not os.path.isfile(ruta) or (w.es_del_sistema(ruta) and
                                                     not ruta.lower().startswith(tareas)):
            return
        if any(a["ruta"] == ruta for a in hecho["archivos"]):
            return
        try:
            qid = q.add(ruta, motivo)
            hecho["archivos"].append({"ruta": w.a_windows(ruta), "id": qid})
            log(f"  cuarentena: {w.a_windows(ruta)}")
        except OSError as e:
            hecho["errores"].append(f"{w.a_windows(ruta)}: {e}")

    for h in elegidos:
        a = h.arreglo or {}
        if "colmena" in a:
            ruta = a["colmena"]
            try:
                if ruta not in editores:
                    os.makedirs(copias, exist_ok=True)
                    nombre = os.path.basename(ruta) + (f"-{h.usuario}" if h.usuario else "")
                    shutil.copy2(ruta, os.path.join(copias, nombre))
                    editores[ruta] = Editor(ruta)
                ed = editores[ruta]
                if a["accion"] == "borrar":
                    ok = ed.borrar_valor(a["clave"], a["valor"])
                elif a["accion"] == "cadena":
                    ok = ed.poner_cadena(a["clave"], a["valor"], a["dato"])
                else:
                    ok = ed.poner_dword(a["clave"], a["valor"], a["dato"])
                if ok:
                    hecho["registro"].append(f"{h.donde}: " + {"borrar": "quitado", "cadena": f"→ «{a.get('dato')}»",
                                                              "dword": f"→ {a.get('dato')}"}[a["accion"]])
                    log(f"  registro: {hecho['registro'][-1]}")
            except (OSError, RegfError) as e:
                hecho["errores"].append(f"{h.donde}: {e}")
        if "hosts" in a:
            try:
                if not any(x.startswith(h.donde) for x in hecho["registro"]):
                    limpiar_hosts(a["hosts"], copias)
                    hecho["registro"].append(f"{h.donde}: líneas peligrosas comentadas")
                    log(f"  hosts: {hecho['registro'][-1]}")
            except OSError as e:
                hecho["errores"].append(f"hosts: {e}")
        if "archivo" in a:
            apartar(a["archivo"], f"{h.tipo}: {h.donde}")
        for f in h.sospechosos:
            apartar(f, f"{h.tipo}: {h.donde}")
    for ruta, ed in editores.items():
        try:
            ed.guardar()
        except OSError as e:
            hecho["errores"].append(f"{ruta}: no se pudo guardar ({e})")
    return hecho


def limpiar_hosts(ruta: str, copias: str) -> None:
    os.makedirs(copias, exist_ok=True)
    shutil.copy2(ruta, os.path.join(copias, "hosts"))
    lineas = open(ruta, "rb").read().decode("latin-1").splitlines()
    sensibles = re.compile(r"(microsoft|windowsupdate|windows\.com|google|bing|facebook|paypal|bank|banco|avast|"
                           r"eset|kaspersky|mcafee|norton|symantec|malwarebytes|bitdefender|virustotal|avira)", re.I)
    out = []
    for l in lineas:
        cuerpo = l.split("#", 1)[0].split()
        if len(cuerpo) >= 2 and any(sensibles.search(n) for n in cuerpo[1:]):
            out.append("# (desactivado por TISMA Escudo) " + l)
        else:
            out.append(l)
    with open(ruta + ".tisma-tmp", "wb") as fh:
        fh.write(("\r\n".join(out) + "\r\n").encode("latin-1"))
    os.replace(ruta + ".tisma-tmp", ruta)


# --------------------------------------------------------------------------------------------------------------
# Discos: encontrar, montar (solo lectura salvo para limpiar) y desmontar
# --------------------------------------------------------------------------------------------------------------
MONTAJES = "/run/escudo/windows"
HIBERNADO = ("Windows está hibernado o se apagó con «inicio rápido» (Windows 8 y posteriores), así que no se puede "
             "escribir en su disco sin perder datos. Arranca Windows y apágalo con «shutdown /s /t 0» (o desactiva "
             "el inicio rápido) y vuelve a limpiar. El análisis sí funciona así.")


def particiones(run=subprocess.run) -> list[dict]:
    r = run(["lsblk", "-J", "-p", "-o", "NAME,FSTYPE,SIZE,LABEL,MOUNTPOINT,TYPE"], capture_output=True, text=True)
    try:
        arbol = json.loads(r.stdout or "{}").get("blockdevices", [])
    except ValueError:
        return []
    out = []

    def recorrer(nodos):
        for n in nodos:
            if (n.get("fstype") or "").lower() in ("ntfs", "bitlocker"):
                out.append(n)
            recorrer(n.get("children") or [])
    recorrer(arbol)
    return out


def montar(dispositivo: str, escritura: bool = False, run=subprocess.run) -> str:
    destino = os.path.join(MONTAJES, re.sub(r"[^A-Za-z0-9_-]", "_", os.path.basename(dispositivo)))
    os.makedirs(destino, mode=0o700, exist_ok=True)
    modo = "rw" if escritura else "ro"
    opciones = f"{modo},nosuid,nodev,noexec"
    orden = (["ntfs-3g", "-o", opciones, dispositivo, destino] if shutil.which("ntfs-3g") else
             ["mount", "-t", "ntfs3", "-o", opciones, dispositivo, destino])
    for intento in range(20):
        r = run(orden, capture_output=True, text=True)
        msg = (r.stderr or r.stdout or "").strip()
        # Tras desmontar, ntfs-3g tarda un momento en soltar el disco («exclusively opened»)
        if r.returncode == 0 or not re.search(r"exclusively opened|busy", msg, re.I):
            break
        time.sleep(0.5)
    if r.returncode != 0:
        try:
            os.rmdir(destino)
        except OSError:
            pass
        if "hibernat" in msg.lower() or r.returncode == 14:
            raise OSError(HIBERNADO)
        raise OSError(f"no se pudo montar {dispositivo}: {msg}")
    # ntfs-3g, ante un Windows hibernado, no falla: monta en solo lectura y sale con 0
    if escritura and os.statvfs(destino).f_flag & os.ST_RDONLY:
        desmontar(destino, run)
        raise OSError(HIBERNADO if ("hibernat" in msg.lower() or "unsafe" in msg.lower()) else
                      f"{dispositivo} solo se pudo montar en solo lectura: {msg}")
    return destino


def desmontar(destino: str, run=subprocess.run) -> None:
    run(["umount", destino], capture_output=True)
    try:
        os.rmdir(destino)
    except OSError:
        pass


# --------------------------------------------------------------------------------------------------------------
# Informe
# --------------------------------------------------------------------------------------------------------------
def informe(w: Windows, hallazgos: list[Hallazgo], todo: bool = False) -> str:
    l = [f"{w.producto} {w.sp}".strip() + f" (versión {w.version}, compilación {w.compilacion}"
         f"{', 64 bits' if w.x64 else ''}) en {w.raiz}",
         f"Usuarios: {', '.join(u['nombre'] for u in w.usuarios) or '—'}"]
    soporte = {"5.1": "XP", "5.2": "XP x64/Server 2003", "6.0": "Vista", "6.1": "7", "6.2": "8", "6.3": "8.1"}
    if w.version in soporte:
        l.append(f"Aviso: Windows {soporte[w.version]} ya no recibe parches de seguridad de Microsoft. El análisis "
                 "limpia lo que encuentre; el kit de endurecimiento de TISMA reduce lo que puede volver a entrar.")
    for a in w.avisos:
        l.append(f"Aviso: {a}")
    cuenta = {g: sum(1 for h in hallazgos if h.gravedad == g) for g in ORDEN}
    l.append(f"\nResultado: {cuenta['high']} graves, {cuenta['medium']} sospechosos, {cuenta['low']} a revisar, "
             f"{cuenta['info']} entradas de inicio normales.")
    for g in ("high", "medium", "low") + (("info",) if todo else ()):
        grupo = [h for h in hallazgos if h.gravedad == g]
        if not grupo:
            continue
        l.append(f"\n== {NOMBRE_GRAV[g]} ({len(grupo)}) ==")
        for h in grupo:
            l.append(f"• {h.donde}" + (f"  [usuario {h.usuario}]" if h.usuario else ""))
            if h.comando:
                l.append(f"    ejecuta: {h.comando}")
            for m in h.motivos:
                l.append(f"    - {m}")
            for d in h.detecciones:
                l.append(f"    - firma: {d}")
    if not any(cuenta[g] for g in ("high", "medium", "low")):
        l.append("\nNo se encontró nada sospechoso.")
    return "\n".join(l)
