"""Diferencias entre sistemas, en un solo sitio.

En Linux (TISMA OS) nada cambia: cada función devuelve exactamente lo de siempre y el aislamiento lo da
bubblewrap (models.jail_engine, tools.Workspace.sandbox_argv, exactas.argv, reducir.jaula).

En Windows NO hay bubblewrap, así que aquí se reúne lo que lo reemplaza, y que es MENOS aislamiento:

* Todo proceso que en Linux va enjaulado (llama-server, el trabajador de cálculo exacto, llama-quantize…) va en un
  objeto de trabajo de Win32 (Job Object) con JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE (si TISMA Code muere, mueren
  con él), límite de memoria y, salvo los comandos del proyecto, ActiveProcessLimit=1 (no pueden crear procesos).
  El proceso se crea SUSPENDIDO, se mete en el Job y solo entonces se reanuda: nunca corre fuera de él. Si el Job
  no se puede crear, el proceso no se lanza (falla cerrado).
* Lo que un Job NO da: ni sistema de archivos de solo lectura ni carpeta personal oculta ni red cortada. Un
  llama-server comprometido por un GGUF malicioso podría leer los archivos del usuario. Por eso:
  - los comandos del proyecto (run_command, verify…) vienen DESACTIVADOS y, si el usuario los activa en Ajustes,
    piden permiso cada vez (nunca «no volver a preguntar»);
  - las funciones que solo existen en TISMA OS (REA, Craft Anything, SolidCraft, Simulación, Strata, Hope, el
    modo sin apuro, los componentes con pkexec) no se ofrecen.
* Textos para el usuario: nunca nombran el sistema («en este equipo»), es una regla del producto.

Todo lo de Win32 se carga con ctypes solo al usarse: este módulo se importa (y se prueba) también en Linux.
"""
from __future__ import annotations

import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

ES_WINDOWS = os.name == "nt"


def _es_enlace(p: str) -> bool:
    """Enlace simbólico o «junction» de Windows (punto de reanálisis): ninguno se sigue al escribir."""
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if os.path.islink(p):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & 0x400)      # FILE_ATTRIBUTE_REPARSE_POINT


def abrir_sin_enlaces(ruta, flags: int, modo: int = 0o600) -> int:
    """os.open que nunca sigue un enlace al crear o escribir (O_NOFOLLOW en Linux). En Windows no existe
    O_NOFOLLOW: se comprueba antes que ni el archivo ni su carpeta sean un enlace o una «junction» (antes daba
    «module 'os' has no attribute 'O_NOFOLLOW'» al instalar especialistas)."""
    ruta = os.fspath(ruta)
    if hasattr(os, "O_NOFOLLOW"):
        return os.open(ruta, flags | os.O_NOFOLLOW, modo)
    if _es_enlace(ruta) or _es_enlace(os.path.dirname(os.path.abspath(ruta))):
        raise OSError(f"no se escribe a través de un enlace: {ruta}")
    return os.open(ruta, flags | getattr(os, "O_BINARY", 0), modo)

MSG_COMANDOS = ("En este equipo los comandos no se ejecutan aislados: actívalos en Ajustes si confías en el "
                "proyecto")
MSG_NO_DISPONIBLE = "No disponible en este equipo"
# Herramientas que en Linux corren en la jaula y en Windows corren SIN aislamiento de archivos ni de red: solo con
# el interruptor de Ajustes encendido y con permiso explícito cada vez
SIN_JAULA = frozenset({"run_command", "verify"})
# Herramientas (esquemas para el modelo) que dependen de programas de TISMA OS: en Windows no se ofrecen
SOLO_LINUX = frozenset({"hope", "hope_ayuda", "consejo", "rea", "rea_modo", "rea_buscar", "rea_guia", "craft",
                        "craft_modo", "craft_buscar", "craft_guia", "solidcraft", "solidcraft_buscar",
                        "solidcraft_guia", "ingenieria", "ingenieria_plantilla", "ingenieria_guia", "jarvis",
                        "formatear", "diagnosticos", "git"})


def _dir_datos_windows(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "TISMA Code"


if ES_WINDOWS:
    DIR_DATOS = _dir_datos_windows()
    DIR_CONFIG = DIR_DATOS / "config"
    # shutil.which (Python ≥ 3.12) y CreateProcess no buscan programas en la carpeta actual: un llama-server.exe
    # plantado en la carpeta desde la que se abrió TISMA Code no se ejecuta en lugar del de verdad
    os.environ.setdefault("NoDefaultCurrentDirectoryInExePath", "1")
    SENALES_SALIDA = tuple(s for s in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGBREAK", None)) if s)
else:
    DIR_DATOS = Path.home() / ".local/share/tisma-code"
    DIR_CONFIG = Path.home() / ".config/tisma-code"
    SENALES_SALIDA = (signal.SIGTERM, signal.SIGHUP)


def capacidades() -> dict:
    """Lo que la interfaz necesita saber de este equipo (sin nombrar el sistema)."""
    return {"aislamiento": not ES_WINDOWS, "sin_apuro": not ES_WINDOWS, "componentes": not ES_WINDOWS,
            "cada_vez": sorted(SIN_JAULA) if ES_WINDOWS else []}


def aprobar_cada_vez(tool: str) -> bool:
    """¿Esta herramienta pide permiso SIEMPRE (sin «no volver a preguntar» ni confianza guardada)?"""
    return ES_WINDOWS and tool in SIN_JAULA


def filtrar_esquemas(esquemas: list[dict]) -> list[dict]:
    if not ES_WINDOWS:
        return esquemas
    return [s for s in esquemas if s["function"]["name"] not in SOLO_LINUX]


def es_archivo_privado(st: os.stat_result) -> bool:
    """Linux: del propio usuario y no escribible por otros. Windows: los permisos son ACL (la carpeta de datos está en
    %LOCALAPPDATA%, solo del usuario); st_uid y los bits de modo no significan nada allí."""
    if ES_WINDOWS:
        return True
    return st.st_uid == os.getuid() and not st.st_mode & 0o022


# ====================================================================== Win32 (ctypes, solo al usarse)
# Estructuras con tipos de ancho fijo (no ctypes.wintypes) para que su tamaño se pueda comprobar en cualquier sistema
# de 64 bits: deben medir lo mismo que en el SDK de Windows x64.
import ctypes  # noqa: E402

_DWORD, _LONG, _ULONGLONG = ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint64
_SIZE_T = ctypes.c_size_t
_HANDLE = ctypes.c_void_p


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", _DWORD), ("MinimumWorkingSetSize", _SIZE_T), ("MaximumWorkingSetSize", _SIZE_T),
                ("ActiveProcessLimit", _DWORD), ("Affinity", _SIZE_T), ("PriorityClass", _DWORD),
                ("SchedulingClass", _DWORD)]


class IO_COUNTERS(ctypes.Structure):  # noqa: N801
    _fields_ = [(n, _ULONGLONG) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                          "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):  # noqa: N801
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", _SIZE_T), ("JobMemoryLimit", _SIZE_T),
                ("PeakProcessMemoryUsed", _SIZE_T), ("PeakJobMemoryUsed", _SIZE_T)]


class JOBOBJECT_BASIC_UI_RESTRICTIONS(ctypes.Structure):  # noqa: N801
    _fields_ = [("UIRestrictionsClass", _DWORD)]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", _DWORD), ("dwMemoryLoad", _DWORD)] + \
        [(n, _ULONGLONG) for n in ("ullTotalPhys", "ullAvailPhys", "ullTotalPageFile", "ullAvailPageFile",
                                   "ullTotalVirtual", "ullAvailVirtual", "ullAvailExtendedVirtual")]


class THREADENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", _DWORD), ("cntUsage", _DWORD), ("th32ThreadID", _DWORD), ("th32OwnerProcessID", _DWORD),
                ("tpBasePri", _LONG), ("tpDeltaPri", _LONG), ("dwFlags", _DWORD)]


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", _DWORD), ("cntUsage", _DWORD), ("th32ProcessID", _DWORD), ("th32DefaultHeapID", _SIZE_T),
                ("th32ModuleID", _DWORD), ("cntThreads", _DWORD), ("th32ParentProcessID", _DWORD),
                ("pcPriClassBase", _LONG), ("dwFlags", _DWORD), ("szExeFile", ctypes.c_uint16 * 260)]


# Constantes del SDK (winnt.h, processthreadsapi.h, tlhelp32.h, iprtrmib.h)
JOB_OBJECT_LIMIT_PROCESS_TIME = 0x00000002
JOB_OBJECT_LIMIT_JOB_TIME = 0x00000004
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x00000100
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JobObjectBasicUIRestrictions = 4
JobObjectExtendedLimitInformation = 9
UILIMIT_HANDLES, UILIMIT_READCLIPBOARD, UILIMIT_WRITECLIPBOARD = 0x1, 0x2, 0x4
UILIMIT_SYSTEMPARAMETERS, UILIMIT_DISPLAYSETTINGS, UILIMIT_GLOBALATOMS = 0x8, 0x10, 0x20
UILIMIT_DESKTOP, UILIMIT_EXITWINDOWS = 0x40, 0x80
# El motor usa la GPU (Vulkan): sin HANDLES ni GLOBALATOMS, que algunos controladores necesitan
UI_MOTOR = (UILIMIT_READCLIPBOARD | UILIMIT_WRITECLIPBOARD | UILIMIT_SYSTEMPARAMETERS | UILIMIT_DISPLAYSETTINGS |
            UILIMIT_DESKTOP | UILIMIT_EXITWINDOWS)
UI_ESTRICTA = UI_MOTOR | UILIMIT_HANDLES | UILIMIT_GLOBALATOMS
CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000
TH32CS_SNAPPROCESS, TH32CS_SNAPTHREAD = 0x2, 0x4
PROCESS_TERMINATE, PROCESS_SET_QUOTA = 0x0001, 0x0100
PROCESS_QUERY_LIMITED_INFORMATION, SYNCHRONIZE = 0x1000, 0x00100000
THREAD_SUSPEND_RESUME = 0x0002
WAIT_TIMEOUT = 0x102
ERROR_INSUFFICIENT_BUFFER = 122
TCP_TABLE_OWNER_PID_LISTENER = 3
_INVALID = ctypes.c_void_p(-1).value

_k32_cache = None


def _k32():
    """kernel32 con los tipos declarados (HANDLE de 64 bits: sin argtypes, ctypes los truncaría a int de 32)."""
    global _k32_cache
    if _k32_cache is not None:
        return _k32_cache
    k = ctypes.WinDLL("kernel32", use_last_error=True)       # type: ignore[attr-defined]
    P = ctypes.POINTER
    sig = {
        "CreateJobObjectW": ([ctypes.c_void_p, ctypes.c_wchar_p], _HANDLE),
        "SetInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD], ctypes.c_int),
        "QueryInformationJobObject": ([_HANDLE, ctypes.c_int, ctypes.c_void_p, _DWORD, P(_DWORD)], ctypes.c_int),
        "AssignProcessToJobObject": ([_HANDLE, _HANDLE], ctypes.c_int),
        "TerminateJobObject": ([_HANDLE, ctypes.c_uint], ctypes.c_int),
        "CloseHandle": ([_HANDLE], ctypes.c_int),
        "OpenProcess": ([_DWORD, ctypes.c_int, _DWORD], _HANDLE),
        "OpenThread": ([_DWORD, ctypes.c_int, _DWORD], _HANDLE),
        "ResumeThread": ([_HANDLE], _DWORD),
        "CreateToolhelp32Snapshot": ([_DWORD, _DWORD], _HANDLE),
        "Thread32First": ([_HANDLE, P(THREADENTRY32)], ctypes.c_int),
        "Thread32Next": ([_HANDLE, P(THREADENTRY32)], ctypes.c_int),
        "Process32FirstW": ([_HANDLE, P(PROCESSENTRY32W)], ctypes.c_int),
        "Process32NextW": ([_HANDLE, P(PROCESSENTRY32W)], ctypes.c_int),
        "QueryFullProcessImageNameW": ([_HANDLE, _DWORD, ctypes.c_wchar_p, P(_DWORD)], ctypes.c_int),
        "WaitForSingleObject": ([_HANDLE, _DWORD], _DWORD),
        "GetProcessTimes": ([_HANDLE, P(_ULONGLONG), P(_ULONGLONG), P(_ULONGLONG), P(_ULONGLONG)], ctypes.c_int),
        "GlobalMemoryStatusEx": ([P(MEMORYSTATUSEX)], ctypes.c_int),
        "GetConsoleWindow": ([], _HANDLE),
    }
    for nombre, (args, res) in sig.items():
        f = getattr(k, nombre)
        f.argtypes, f.restype = args, res
    _k32_cache = k
    return k


def _error(que: str) -> OSError:
    return OSError(ctypes.get_last_error() if ES_WINDOWS else 0, f"{que} falló")   # type: ignore[attr-defined]


# ---------------------------------------------------------------- memoria
def ram_total_mb() -> int:
    if not ES_WINDOWS:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) // 1024
        return 0
    m = MEMORYSTATUSEX()
    m.dwLength = ctypes.sizeof(m)
    if not _k32().GlobalMemoryStatusEx(ctypes.byref(m)):
        return 0
    return int(m.ullTotalPhys // (1 << 20))


def _memoria_registro(k, winreg) -> int:
    """qwMemorySize (QWORD, controladores actuales) o MemorySize (DWORD o binario, hasta 4 GB, los viejos)."""
    for valor in ("HardwareInformation.qwMemorySize", "HardwareInformation.MemorySize"):
        try:
            v = winreg.QueryValueEx(k, valor)[0]
        except OSError:
            continue
        return int.from_bytes(v[:8], "little") if isinstance(v, (bytes, bytearray)) else int(v)
    raise OSError("sin tamaño de memoria")


def gpus_windows() -> list[dict]:
    """GPU con memoria propia según el registro (clase «Display»: HardwareInformation.qwMemorySize, que escribe el
    controlador). Solo AMD/Intel: las NVIDIA ya las da nvidia-smi."""
    if not ES_WINDOWS:
        return []
    import winreg  # type: ignore[import-not-found]
    clave = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
    out = []
    try:
        raiz = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, clave)
    except OSError:
        return []
    with raiz:
        for i in range(64):
            try:
                sub = winreg.EnumKey(raiz, i)
            except OSError:
                break
            if not sub.isdigit():
                continue
            try:
                with winreg.OpenKey(raiz, sub) as k:
                    nombre = str(winreg.QueryValueEx(k, "DriverDesc")[0])
                    mem = _memoria_registro(k, winreg)
            except (OSError, ValueError, TypeError):
                continue
            mb = mem // (1 << 20)
            # Igual que en Linux: una «VRAM» chica es la reserva de RAM de una gráfica integrada
            if mb > 2048 and "nvidia" not in nombre.lower():
                out.append({"name": nombre, "vram_mb": mb})
    return out


class SYSTEM_POWER_STATUS(ctypes.Structure):  # noqa: N801
    _fields_ = [("ACLineStatus", ctypes.c_uint8), ("BatteryFlag", ctypes.c_uint8),
                ("BatteryLifePercent", ctypes.c_uint8), ("SystemStatusFlag", ctypes.c_uint8),
                ("BatteryLifeTime", _DWORD), ("BatteryFullLifeTime", _DWORD)]


def enchufado_segun(st: SYSTEM_POWER_STATUS) -> bool:
    """Con corriente, sin batería (128) o sin saber (255): como en Linux, solo «a batería» cuenta como no enchufado."""
    return st.ACLineStatus != 0 or bool(st.BatteryFlag & 128)


def enchufado() -> bool:
    st = SYSTEM_POWER_STATUS()
    k = ctypes.WinDLL("kernel32", use_last_error=True)       # type: ignore[attr-defined]
    k.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
    if not k.GetSystemPowerStatus(ctypes.byref(st)):
        return True
    return enchufado_segun(st)


# ---------------------------------------------------------------- objetos de trabajo (Job Objects)
@dataclass(frozen=True)
class Limites:
    memoria: int = 0          # bytes por proceso (0 = sin límite)
    memoria_job: int = 0      # bytes entre todos los procesos del Job
    cpu_s: int = 0            # tiempo de CPU de usuario por proceso
    cpu_job_s: int = 0        # tiempo de CPU de usuario de todo el Job
    procesos: int = 0         # procesos vivos a la vez (1 = no puede crear otros)
    ui: int = UI_MOTOR


def valores_job(lim: Limites) -> tuple[JOBOBJECT_EXTENDED_LIMIT_INFORMATION, int]:
    """La estructura que se pasa a SetInformationJobObject (aparte, para poder comprobarla en cualquier sistema)."""
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    b = info.BasicLimitInformation
    flags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
    if lim.memoria:
        flags |= JOB_OBJECT_LIMIT_PROCESS_MEMORY
        info.ProcessMemoryLimit = lim.memoria
    if lim.memoria_job:
        flags |= JOB_OBJECT_LIMIT_JOB_MEMORY
        info.JobMemoryLimit = lim.memoria_job
    if lim.cpu_s:
        flags |= JOB_OBJECT_LIMIT_PROCESS_TIME
        b.PerProcessUserTimeLimit = lim.cpu_s * 10_000_000          # en unidades de 100 ns
    if lim.cpu_job_s:
        flags |= JOB_OBJECT_LIMIT_JOB_TIME
        b.PerJobUserTimeLimit = lim.cpu_job_s * 10_000_000
    if lim.procesos:
        flags |= JOB_OBJECT_LIMIT_ACTIVE_PROCESS
        b.ActiveProcessLimit = lim.procesos
    b.LimitFlags = flags
    return info, lim.ui


def crear_job(lim: Limites) -> int:
    """Crea el Job con sus límites. Cualquier fallo: OSError (y el llamador no lanza nada)."""
    k = _k32()
    h = k.CreateJobObjectW(None, None)
    if not h:
        raise _error("CreateJobObject")
    info, ui = valores_job(lim)
    ok = k.SetInformationJobObject(h, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info))
    if ok and ui:
        r = JOBOBJECT_BASIC_UI_RESTRICTIONS(ui)
        ok = k.SetInformationJobObject(h, JobObjectBasicUIRestrictions, ctypes.byref(r), ctypes.sizeof(r))
    if not ok:
        err = _error("SetInformationJobObject")
        k.CloseHandle(h)
        raise err
    return h


def _reanudar(pid: int) -> int:
    """Reanuda los hilos de un proceso creado con CREATE_SUSPENDED (subprocess no conserva el del hilo principal)."""
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snap or snap == _INVALID:
        raise _error("CreateToolhelp32Snapshot")
    n = 0
    try:
        te = THREADENTRY32()
        te.dwSize = ctypes.sizeof(te)
        ok = k.Thread32First(snap, ctypes.byref(te))
        while ok:
            if te.th32OwnerProcessID == pid:
                h = k.OpenThread(THREAD_SUSPEND_RESUME, False, te.th32ThreadID)
                if h:
                    if k.ResumeThread(h) != 0xFFFFFFFF:
                        n += 1
                    k.CloseHandle(h)
            ok = k.Thread32Next(snap, ctypes.byref(te))
    finally:
        k.CloseHandle(snap)
    if not n:
        raise OSError("no se pudo reanudar el proceso")
    return n


# Procesos lanzados por TISMA Code (pid → Popen). Mientras su Popen vive, Windows no reutiliza el pid: «es mío» se
# decide con esta lista, no con nombres de proceso.
_PROPIOS: dict[int, subprocess.Popen] = {}
_MOTORES: set[int] = set()
_lock = threading.Lock()


def propios_vivos() -> set[int]:
    with _lock:
        for pid, p in list(_PROPIOS.items()):
            if p.poll() is not None:
                _PROPIOS.pop(pid, None)
                _MOTORES.discard(pid)
        return set(_PROPIOS)


def _hay_consola() -> bool:
    try:
        return bool(_k32().GetConsoleWindow())
    except (OSError, AttributeError):
        return False


def resolver(exe: str) -> str:
    """Ruta completa de un programa (sin buscar en la carpeta actual)."""
    if os.path.isabs(exe):
        return exe
    import shutil
    r = shutil.which(exe)
    if not r:
        raise FileNotFoundError(f"{exe} no está instalado")
    return r


def popen_en_job(argv, lim: Limites, *, nuevo_grupo: bool = False, motor: bool = False, **kw) -> subprocess.Popen:
    """Popen dentro de un Job: el proceso nace suspendido, se asigna al Job y recién entonces arranca. Si algo de eso
    falla, se mata y se lanza OSError: nunca queda corriendo sin su Job."""
    if not ES_WINDOWS:
        raise RuntimeError("popen_en_job solo existe en Windows")
    if isinstance(argv, (list, tuple)):
        argv = [resolver(argv[0]), *argv[1:]]
    job = crear_job(lim)
    k = _k32()
    flags = kw.pop("creationflags", 0) | CREATE_SUSPENDED
    if nuevo_grupo:
        flags |= CREATE_NEW_PROCESS_GROUP
    if not _hay_consola():
        flags |= CREATE_NO_WINDOW            # sin consola propia (pythonw): que no aparezca una ventana negra
    try:
        proc = subprocess.Popen(argv, creationflags=flags, **kw)
    except BaseException:
        k.CloseHandle(job)
        raise
    try:
        h = k.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, proc.pid)
        if not h:
            raise _error("OpenProcess")
        try:
            if not k.AssignProcessToJobObject(job, h):
                raise _error("AssignProcessToJobObject")
        finally:
            k.CloseHandle(h)
        _reanudar(proc.pid)
    except BaseException:
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass
        k.TerminateJobObject(job, 1)
        k.CloseHandle(job)
        raise
    proc._tisma_job = job                    # type: ignore[attr-defined]
    with _lock:
        _PROPIOS[proc.pid] = proc
        if motor:
            _MOTORES.add(proc.pid)
    return proc


def cerrar_job(proc: subprocess.Popen) -> None:
    """Termina todo lo que quede en el Job del proceso y libera el Job (idempotente)."""
    job = getattr(proc, "_tisma_job", None)
    if job:
        proc._tisma_job = None               # type: ignore[attr-defined]
        k = _k32()
        k.TerminateJobObject(job, 1)
        k.CloseHandle(job)
    with _lock:
        if proc.poll() is not None:
            _PROPIOS.pop(proc.pid, None)
            _MOTORES.discard(proc.pid)


def entorno_minimo(extra: dict | None = None, claves=("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT",
                                                       "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")) -> dict:
    """Solo variables del sistema, nunca las del usuario (tokens, claves de API…)."""
    env = {}
    for k, v in os.environ.items():
        if k.upper() in claves:
            env[k.upper()] = v
    env.update(extra or {})
    return env


# ---------------------------------------------------------------- motor (llama-server)
def lanzar_motor(argv: list[str]) -> subprocess.Popen:
    """llama-server en Windows. MENOS aislado que en Linux: allí bubblewrap le oculta la carpeta personal y deja el
    sistema de solo lectura; aquí solo hay un Job (muere con TISMA Code, no puede crear procesos, memoria acotada a la
    RAM del equipo, sin portapapeles), escucha solo en 127.0.0.1 y exige la clave aleatoria de cada arranque
    (--api-key). Un GGUF malicioso que lograra ejecutar código leería lo que el usuario puede leer.
    Grupo de procesos propio: así se le puede mandar Ctrl+Break sin tocar a TISMA Code ni a otros motores."""
    cache = DIR_DATOS / "cache-motor"
    cache.mkdir(parents=True, exist_ok=True)
    ram = ram_total_mb() << 20
    lim = Limites(memoria=ram, procesos=1, ui=UI_MOTOR)
    env = entorno_minimo({"TEMP": str(cache), "TMP": str(cache)})
    return popen_en_job(argv, lim, nuevo_grupo=True, motor=True, stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, cwd=str(cache), env=env)


def cerrar_motor(proc: subprocess.Popen, espera: float = 15) -> None:
    """Ctrl+Break al grupo del motor y espera a que salga; después, el Job entero (se termina sí o sí).
    Nota (sin verificar en hardware): las compilaciones de llama-server para Windows atienden Ctrl+C en su manejador
    de consola; a Ctrl+Break puede responder saliendo sin guardar la caché de n-gramas, igual que al cerrarlo de golpe.
    Ctrl+C no se puede dirigir a un solo grupo de procesos (GenerateConsoleCtrlEvent), por eso se usa Ctrl+Break."""
    if proc.poll() is None:
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)     # type: ignore[attr-defined]
            enviado = True
        except (OSError, AttributeError, ValueError):
            enviado = False
        if enviado:
            try:
                proc.wait(espera)
            except subprocess.TimeoutExpired:
                pass
    cerrar_job(proc)
    if proc.poll() is None:
        try:
            proc.kill()
            proc.wait(5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def cerrar_motores(espera: float = 15) -> int:
    """Al salir: todos los motores lanzados por esta app, en orden, y después sus Jobs."""
    with _lock:
        motores = [_PROPIOS[p] for p in _MOTORES if p in _PROPIOS and _PROPIOS[p].poll() is None]
    for p in motores:
        try:
            os.kill(p.pid, signal.CTRL_BREAK_EVENT)        # type: ignore[attr-defined]
        except (OSError, AttributeError, ValueError):
            pass
    fin = time.monotonic() + espera
    while time.monotonic() < fin and any(p.poll() is None for p in motores):
        time.sleep(0.2)
    with _lock:
        todos = list(_PROPIOS.values())
    for p in todos:
        cerrar_job(p)
    return len(motores)


# ---------------------------------------------------------------- trabajos aislados (cálculo, reducir, comandos)
def ejecutar_en_job(argv, lim: Limites, *, entrada: str | None = None, timeout: float, cwd=None, env=None,
                    encoding: str = "utf-8") -> subprocess.CompletedProcess:
    """Como subprocess.run, pero dentro de un Job: al agotarse el tiempo se termina TODO el Job (también los procesos
    que haya creado), no solo el primero. Lanza subprocess.TimeoutExpired como subprocess.run."""
    proc = popen_en_job(argv, lim, stdin=subprocess.PIPE if entrada is not None else subprocess.DEVNULL,
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=cwd, env=env, text=True,
                        encoding=encoding, errors="replace")
    try:
        out, err = proc.communicate(entrada, timeout=timeout)
    except subprocess.TimeoutExpired:
        cerrar_job(proc)
        proc.kill()
        proc.communicate()
        raise
    finally:
        cerrar_job(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


TRABAJADOR_MEMORIA = 3 << 30


def python_consola() -> str:
    """python.exe junto al intérprete actual (pythonw.exe no tiene salida estándar fiable)."""
    exe = Path(sys.executable)
    cand = exe.with_name("python.exe")
    return str(cand if cand.is_file() else exe)


def ejecutar_trabajador(worker: Path, raiz: Path, pedido: str, tiempo: int) -> subprocess.CompletedProcess:
    """exactas_worker.py en Windows: Job con un único proceso (no puede lanzar otros), 3 GiB, `tiempo` s de CPU,
    restricciones de interfaz y un entorno sin las variables del usuario. Si el Job no se puede crear, se ejecuta sin
    él y el propio trabajador lo detecta (no está en un Job restringido) y rechaza la vía de sympy (parse_expr usa
    eval: allí el aislamiento es la segunda defensa tras el filtro de la entrada)."""
    tmp = DIR_DATOS / "tmp-calculo"
    tmp.mkdir(parents=True, exist_ok=True)
    env = entorno_minimo({"OMP_NUM_THREADS": "2", "MPLCONFIGDIR": str(tmp), "TEMP": str(tmp), "TMP": str(tmp)},
                         claves=("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "NUMBER_OF_PROCESSORS",
                                 "PROCESSOR_ARCHITECTURE", "OS"))
    argv = [python_consola(), "-X", "utf8", "-I", str(worker)]
    lim = Limites(memoria=TRABAJADOR_MEMORIA, cpu_s=tiempo, procesos=1, ui=UI_ESTRICTA)
    try:
        job = crear_job(lim)               # ¿se puede crear un Job en este equipo?
    except OSError:
        job = None
    if job:
        _k32().CloseHandle(job)
        return ejecutar_en_job(argv, lim, entrada=pedido, timeout=tiempo + 10, cwd=str(raiz), env=env)
    # Sin Job: solo el filtro de la entrada; el trabajador se niega a usar parse_expr (ver jail_activo en el trabajador)
    return subprocess.run(argv, input=pedido, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=tiempo + 10, cwd=str(raiz), env=env,
                          creationflags=0 if _hay_consola() else CREATE_NO_WINDOW)


def lanzar_herramienta(argv: list[str], trabajo: Path) -> subprocess.Popen:
    """llama-quantize / llama-imatrix / llama-perplexity (reducir): Job con un único proceso y memoria acotada a la
    RAM. Como el motor, leen GGUF de internet; aquí no se les ocultan los archivos del usuario (ver lanzar_motor)."""
    lim = Limites(memoria=ram_total_mb() << 20, procesos=1, ui=UI_ESTRICTA)
    env = entorno_minimo({"TEMP": str(trabajo), "TMP": str(trabajo)})
    return popen_en_job(argv, lim, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        cwd=str(trabajo), env=env)


CLAVES_COMANDO = ("SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "TEMP", "TMP", "USERPROFILE",
                  "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
                  "PROGRAMFILES(X86)", "PROGRAMW6432", "COMMONPROGRAMFILES", "COMMONPROGRAMFILES(X86)",
                  "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS", "USERNAME")


def linea_cmd(command: str) -> str:
    """cmd.exe /d (sin AutoRun del registro) /s /c "orden": con /s, cmd quita solo las comillas exteriores y ejecuta
    la orden tal cual (una lista de argumentos se escaparía con barras invertidas, que cmd no entiende)."""
    cmd = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "cmd.exe")
    return f'"{cmd}" /d /s /c "{command}"'


def ejecutar_comando(command: str, cwd: Path, timeout: int) -> subprocess.CompletedProcess:
    """Una orden del proyecto en Windows, SOLO si el usuario los activó y la aprobó (lo comprueba quien llama).
    Job: muere con TISMA Code, tiempo de CPU y memoria acotados entre todos sus procesos; al agotarse el tiempo se
    termina el árbol entero. SIN aislamiento de archivos ni de red (lo dice el interruptor de Ajustes)."""
    ram = ram_total_mb() << 20
    lim = Limites(memoria_job=max(1 << 30, ram // 2), cpu_job_s=timeout * max(1, os.cpu_count() or 1),
                  procesos=64, ui=UI_MOTOR)
    env = entorno_minimo({"PYTHONDONTWRITEBYTECODE": "1"}, claves=CLAVES_COMANDO)
    return ejecutar_en_job(linea_cmd(command), lim, timeout=timeout, cwd=str(cwd), env=env, encoding="oem")


# ---------------------------------------------------------------- procesos
def nombre_proceso(pid: int) -> str:
    k = _k32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        n = _DWORD(len(buf))
        if not k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
            return ""
        nombre = os.path.basename(buf.value)
        return nombre[:-4] if nombre.lower().endswith(".exe") else nombre
    finally:
        k.CloseHandle(h)


def proceso_vivo(pid: int) -> bool:
    k = _k32()
    h = k.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k.CloseHandle(h)


def _creado(pid: int) -> int:
    k = _k32()
    h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return 0
    try:
        c, e, kk, u = (_ULONGLONG() for _ in range(4))
        return c.value if k.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(kk), ctypes.byref(u)) else 0
    finally:
        k.CloseHandle(h)


def _padres() -> dict[int, int]:
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID:
        return {}
    out = {}
    try:
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(pe)
        ok = k.Process32FirstW(snap, ctypes.byref(pe))
        while ok:
            out[pe.th32ProcessID] = pe.th32ParentProcessID
            ok = k.Process32NextW(snap, ctypes.byref(pe))
    finally:
        k.CloseHandle(snap)
    return out


def hijos_de(pid: int, padres: dict[int, int], creado=None) -> list[int]:
    """Descendientes de `pid` (del más cercano al más lejano). Windows no avisa si el padre murió y su pid se
    reutilizó: un «hijo» creado ANTES que su supuesto padre no lo es."""
    creado = creado or (lambda p: 0)
    out, pendientes = [], [pid]
    while pendientes:
        p = pendientes.pop(0)
        tp = creado(p)
        for h, padre in padres.items():
            if padre == p and h != p and h not in out and creado(h) >= tp:
                out.append(h)
                pendientes.append(h)
    return out


def descendientes(pid: int) -> list[int]:
    return hijos_de(pid, _padres(), _creado)


# ---------------------------------------------------------------- puertos
def filas_tcp4(raw: bytes) -> list[tuple[str, int, int]]:
    """MIB_TCPTABLE_OWNER_PID → [(dirección, puerto, pid)]. dwLocalPort va en orden de red en los 16 bits bajos."""
    n = struct.unpack_from("<I", raw, 0)[0]
    out = []
    for i in range(n):
        _estado, addr, port, _ra, _rp, pid = struct.unpack_from("<6I", raw, 4 + i * 24)
        out.append((socket.inet_ntoa(struct.pack("<I", addr)), ((port & 0xFF) << 8) | ((port >> 8) & 0xFF), pid))
    return out


def filas_tcp6(raw: bytes) -> list[tuple[str, int, int]]:
    """MIB_TCP6TABLE_OWNER_PID: ucLocalAddr[16], scope, puerto, ucRemoteAddr[16], scope, puerto, estado, pid."""
    n = struct.unpack_from("<I", raw, 0)[0]
    out = []
    for i in range(n):
        o = 4 + i * 56
        addr = raw[o:o + 16]
        port = struct.unpack_from("<I", raw, o + 20)[0]
        pid = struct.unpack_from("<I", raw, o + 52)[0]
        out.append((socket.inet_ntop(socket.AF_INET6, addr), ((port & 0xFF) << 8) | ((port >> 8) & 0xFF), pid))
    return out


def _tabla_tcp(af: int) -> bytes:
    ip = ctypes.WinDLL("iphlpapi")                         # type: ignore[attr-defined]
    f = ip.GetExtendedTcpTable
    f.argtypes = [ctypes.c_void_p, ctypes.POINTER(_DWORD), ctypes.c_int, ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    f.restype = _DWORD
    tam = _DWORD(1 << 16)
    for _ in range(6):
        buf = ctypes.create_string_buffer(tam.value)
        r = f(buf, ctypes.byref(tam), 0, af, TCP_TABLE_OWNER_PID_LISTENER, 0)
        if r == 0:
            return buf.raw
        if r != ERROR_INSUFFICIENT_BUFFER:
            raise OSError(r, "GetExtendedTcpTable falló")
    raise OSError("GetExtendedTcpTable: la tabla no deja de crecer")


def escuchando(port: int) -> list[tuple[str, int]]:
    """[(dirección, pid)] de los procesos que escuchan en ese puerto TCP (IPv4 e IPv6)."""
    out = []
    for af, filas in ((socket.AF_INET, filas_tcp4), (socket.AF_INET6, filas_tcp6)):
        try:
            out += [(a, pid) for a, p, pid in filas(_tabla_tcp(int(af))) if p == port]
        except OSError:
            continue
    return out


def puerto_escucha(port: int) -> bool:
    try:
        return bool(escuchando(port))
    except Exception:  # noqa: BLE001 — sin la tabla: se prueba a conectar
        pass
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def puerto_es_mio(port: int, oyentes=None, propios=None) -> bool:
    """¿TODO lo que escucha en ese puerto es un proceso lanzado por TISMA Code? En Windows otro programa podría
    adelantarse en el puerto (o compartirlo con SO_REUSEADDR) y dictarle llamadas a herramientas al agente: si hay
    un solo oyente ajeno, no es el motor."""
    oyentes = escuchando(port) if oyentes is None else oyentes
    propios = propios_vivos() if propios is None else propios
    return bool(oyentes) and all(pid in propios for _a, pid in oyentes)


# ---------------------------------------------------------------- ventana de la app
def candidatos_navegador(env=None) -> list[str]:
    env = os.environ if env is None else env
    out = []
    for var in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        base = env.get(var)
        if base:
            out.append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
    for var in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = env.get(var)
        if base:
            out.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    return out


def abrir_ventana_app(url: str, perfil: Path, existe=os.path.isfile, lanzar=subprocess.Popen):
    """Ventana propia (sin pestañas ni barra de direcciones): Edge o Chrome con --app y un perfil aparte (así el
    proceso vive mientras la ventana está abierta). Si no hay ninguno, el navegador predeterminado (devuelve None).
    La URL lleva el token de un solo uso, como en Linux."""
    for exe in candidatos_navegador():
        if not existe(exe):
            continue
        try:
            perfil.mkdir(parents=True, exist_ok=True)
            return lanzar([exe, f"--app={url}", f"--user-data-dir={perfil}", "--no-first-run",
                           "--no-default-browser-check", "--window-size=1280,860"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            continue
    try:
        os.startfile(url)                                  # type: ignore[attr-defined]
    except (OSError, AttributeError):
        import webbrowser
        webbrowser.open(url)
    return None
