"""tisma-adapt (documento de traspaso, §3-§5): política = f(perfil firmado del equipo, clase de carga, modo).

* Solo actúa si hay un perfil de equipo VÁLIDO Y FIRMADO (perfil.cargar). Sin él no hace nada: valores de Debian.
* Clase de carga, por orden de confianza: (1) lo declarado (modo IA de TISMA Code, juegos lanzados por Steam/Wine),
  (2) señales del sistema (uso de CPU, procesos de compilación o de vídeo), (3) heurística (inactivo/interactivo).
  Con HISTÉRESIS: la clase nueva tiene que sostenerse varios segundos antes de cambiar de política.
* Aplicación por la lista blanca de rutas de centinela (Actuator), comprobando antes que cada ruta exista.
* Un ajuste nuevo (nunca validado en este equipo) se prueba: si en la ventana de prueba aparecen errores del
  kernel de su componente, se revierte y va a la lista negra LOCAL; si no, queda validado.
* Límites duros: Máximo baja a Normal mientras la temperatura sostenida supere el techo del perfil.
* Contador de arranques fallidos: dos arranques seguidos sin llegar al escritorio con el perfil → se suspende el
  perfil (valores de Debian) hasta que haya uno nuevo.
Lo que no es de /sys (hilos e IA, zram, planificador, tareas de fondo) se publica en /run/centinela/politica.json
para quien lo use (TISMA Code, servicios); no se aplica aquí.
"""
from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import perfil as P
from . import sysfs
from .apply import STATE_DIR, Actuator

POLITICA = Path(os.environ.get("CENTINELA_RUN", "/run/centinela")) / "politica.json"
NEGRA = Path(STATE_DIR) / "lista-negra.json"
VALIDADOS = Path(STATE_DIR) / "ajustes-validados.json"
ARRANQUES = Path(STATE_DIR) / "arranques.json"
PERFILES = ["/etc/centinela/perfil.json"] + sorted(glob.glob("/usr/share/tisma/perfiles/*.json"))
MODO = {"silencioso": "eco", "equilibrado": "normal", "rendimiento": "maximo"}
COMPILAR = re.compile(r"^(cc1|cc1plus|rustc|ld|ld\.lld|ninja|javac|go|cargo|clang(\+\+)?)$")
VIDEO = re.compile(r"^(haruna|mpv|vlc|totem|celluloid|elisa|ffplay|obs)$")
# Qué errores del kernel delatan a cada ajuste (para revertirlo)
COMPONENTE = {"pcie.aspm": r"pcie|aer|pci .*error", "sata.alpm": r"ata\d.*(error|reset|timeout)",
              "gpu.dpm": r"amdgpu.*(timeout|reset|fault)|radeon.*(lockup|reset)|i915.*hang",
              "usb.autosuspend_s": r"usb .*(reset|disconnect|error)", "audio.power_save_s": r"snd_hda.*(timeout|error)",
              "cpu.modulos_activos": r"(soft|hard) lockup|rcu.*stall", "nvme.apst_latencia_us": r"nvme.*(timeout|reset)"}


# ---------------------------------------------------------------- ajuste → escrituras en /sys y /proc
def escrituras(clave: str, valor) -> list[tuple[str, str]]:
    """Rutas y valores para un ajuste del vocabulario; solo las que existen en ESTE equipo."""
    out: list[tuple[str, str]] = []
    cpus = sysfs.ls("/sys/devices/system/cpu/cpu[0-9]*")
    if clave == "cpu.governor":
        disp = (sysfs.read("/sys/devices/system/cpu/cpu0/cpufreq/scaling_available_governors", "") or "").split()
        if valor in disp:
            out = [(f"{c}/cpufreq/scaling_governor", valor) for c in cpus
                   if sysfs.read(f"{c}/cpufreq/scaling_governor") is not None]
    elif clave == "cpu.boost":
        out = [("/sys/devices/system/cpu/cpufreq/boost", str(valor))]
    elif clave == "cpu.epp":
        out = [(f"{c}/cpufreq/energy_performance_preference", valor) for c in cpus
               if sysfs.read(f"{c}/cpufreq/energy_performance_preference") is not None]
    elif clave == "cpu.modulos_activos":
        # Módulos CMT = grupos de hermanos (thread_siblings_list); se dejan los N primeros, nunca el de la CPU 0
        modulo_de = {}
        for c in cpus:
            n = int(re.search(r"cpu(\d+)$", c).group(1))
            modulo_de[n] = sysfs.read(f"{c}/topology/thread_siblings_list") or str(n)
        orden = sorted(set(modulo_de.values()), key=lambda m: min(k for k, v in modulo_de.items() if v == m))
        quedan = set(orden[:max(1, int(valor))])
        out = [(f"/sys/devices/system/cpu/cpu{n}/online", "0") for n, m in sorted(modulo_de.items())
               if m not in quedan and n != 0]
    elif clave == "pcie.aspm":
        out = [("/sys/module/pcie_aspm/parameters/policy", valor)]
    elif clave == "sata.alpm":
        out = [(f"{h}/link_power_management_policy", valor) for h in sysfs.ls("/sys/class/scsi_host/host*")]
    elif clave == "gpu.dpm":
        out = [(f"{c}/device/power_dpm_force_performance_level", valor) for c in sysfs.ls("/sys/class/drm/card[0-9]")]
    elif clave == "usb.autosuspend_s":
        ms = "-1000" if valor < 0 else str(valor * 1000)
        out = [(f"{d}/power/autosuspend_delay_ms", ms) for d in sysfs.ls("/sys/bus/usb/devices/*")]
    elif clave == "audio.power_save_s":
        out = [("/sys/module/snd_hda_intel/parameters/power_save", str(valor))]
    elif clave == "workqueue.power_efficient":
        out = [("/sys/module/workqueue/parameters/power_efficient", "Y" if valor else "N")]
    elif clave.startswith("vm."):
        out = [("/proc/sys/vm/" + clave[3:].replace("page_cluster", "page-cluster"), str(valor))]
    elif clave == "thp":
        out = [("/sys/kernel/mm/transparent_hugepage/enabled", valor)]
    return [(p, v) for p, v in out if sysfs.read(p) is not None]


def _elegido(v: str | None) -> str | None:
    """sysfs muestra las opciones con la elegida entre corchetes («[default] powersave»): se guarda esa sola,
    que es lo que se puede volver a escribir."""
    if v and "[" in v:
        m = re.search(r"\[([^\]]+)\]", v)
        return m.group(1) if m else v
    return v


def _leer_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def _guardar_json(p: Path, d: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1))
    os.replace(tmp, p)


# ---------------------------------------------------------------- clase de carga con histéresis
class Clasificador:
    def __init__(self, sostener_s: float = 15, reloj=time.monotonic):
        self.sostener_s, self.reloj = sostener_s, reloj
        self.clase = "interactivo"
        self._cand, self._desde = None, 0.0

    @staticmethod
    def candidata(ocupado: float, cargas: set[str], ia: bool, procesos: set[str]) -> str:
        if ia:                                            # (1) declarado
            return "ia"
        if "juego" in cargas:
            return "juego"
        if any(COMPILAR.match(p) for p in procesos) and ocupado > 0.5:      # (2) señales
            return "lote"
        if any(VIDEO.match(p) for p in procesos):
            return "multimedia"
        return "inactivo" if ocupado < 0.05 else "interactivo"              # (3) heurística

    def paso(self, cand: str) -> str:
        """Cambia de clase solo si la candidata se sostiene `sostener_s` segundos (un cambio a la vez)."""
        ahora = self.reloj()
        if cand == self.clase:
            self._cand = None
        elif cand != self._cand:
            self._cand, self._desde = cand, ahora
        elif ahora - self._desde >= self.sostener_s:
            self.clase, self._cand = cand, None
        return self.clase


def procesos_activos() -> set[str]:
    out = set()
    for d in glob.glob("/proc/[0-9]*/comm"):
        try:
            out.add(Path(d).read_text().strip())
        except OSError:
            pass
    return out


# ---------------------------------------------------------------- adaptador
class Adaptador:
    def __init__(self, perfil: dict, act: Actuator | None = None, reloj=time.monotonic, prueba_s: float = 120,
                 errores_kernel=None, temperatura=None):
        self.p, self.act, self.reloj = perfil, act or Actuator(), reloj
        self.prueba_s = prueba_s
        self.clasif = Clasificador(reloj=reloj)
        self.aplicado: dict[str, object] = {}
        self.en_prueba: dict[str, tuple[float, dict]] = {}     # ajuste → (inicio, valores anteriores)
        self.errores_kernel = errores_kernel or self._errores_kernel
        self.temperatura = temperatura or self._temp
        self.caliente_desde: float | None = None
        self.modo_termico: str | None = None

    @staticmethod
    def _temp():
        from .calor import temperatura_cpu
        return temperatura_cpu()

    @staticmethod
    def _errores_kernel(segundos: float) -> str:
        try:
            return subprocess.run(["journalctl", "-k", "-q", "--no-pager", "-o", "cat", "--since",
                                   f"-{int(segundos) + 1}s"], capture_output=True, text=True, timeout=20).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def modo(self, preferido: str) -> str:
        """Límite duro: Máximo baja a Normal mientras la temperatura sostenida (60 s) supere el techo."""
        techo = self.p["limites_duros"]["temp_max_c"]
        t = self.temperatura()
        ahora = self.reloj()
        if t is not None and t >= techo:
            if self.caliente_desde is None:
                self.caliente_desde = ahora
            if ahora - self.caliente_desde >= 60 and preferido == "maximo":
                self.modo_termico = "normal"
        elif t is not None and t < techo - 10:
            self.caliente_desde, self.modo_termico = None, None
        return self.modo_termico or preferido

    def tick(self, preferido: str, agresivo: bool, ocupado: float, cargas: set[str], ia: bool,
             procesos: set[str]) -> dict:
        clase = self.clasif.paso(Clasificador.candidata(ocupado, cargas, ia, procesos))
        modo = self.modo(preferido)
        validados = set(_leer_json(VALIDADOS).get("ajustes", []))
        self._revisar_pruebas(validados)                   # primero: lo que falló entra a la lista negra
        negra = set(_leer_json(NEGRA).get("ajustes", []))
        deseados = P.ajustes_de(self.p, modo, clase, agresivo, negra)
        for clave, valor in deseados.items():
            if self.aplicado.get(clave) == valor:
                continue
            nuevo = f"{clave}={valor}" not in validados
            if nuevo and self.en_prueba:
                continue                                   # un ajuste nuevo a la vez
            ws = escrituras(clave, valor)
            antes = {p: _elegido(sysfs.read(p)) for p, _ in ws}
            for p, v in ws:
                self.act.write(p, v)
            self.aplicado[clave] = valor
            if nuevo and ws:
                self.en_prueba[f"{clave}={valor}"] = (self.reloj(), antes)
        pol = {"modo": modo, "clase": clase, "agresivo": agresivo, "ajustes": deseados,
               "en_prueba": sorted(self.en_prueba)}
        try:
            _guardar_json(POLITICA, pol)
        except OSError:
            pass
        return pol

    def _revisar_pruebas(self, validados: set[str]) -> None:
        for aj, (inicio, antes) in list(self.en_prueba.items()):
            clave = aj.split("=", 1)[0]
            log = self.errores_kernel(self.reloj() - inicio)
            patron = COMPONENTE.get(clave)
            if patron and re.search(patron, log, re.I):
                for p, v in antes.items():                 # revertir y no volver a usarlo en este equipo
                    if v is not None:
                        self.act.write(p, v)
                self.aplicado.pop(clave, None)
                negra = _leer_json(NEGRA)
                negra.setdefault("ajustes", []).append(aj)
                _guardar_json(NEGRA, negra)
                del self.en_prueba[aj]
            elif self.reloj() - inicio >= self.prueba_s:
                validados.add(aj)
                _guardar_json(VALIDADOS, {"ajustes": sorted(validados)})
                del self.en_prueba[aj]


# ---------------------------------------------------------------- perfil del equipo y arranques fallidos
def perfil_activo() -> dict | None:
    """El perfil firmado de este equipo, o None (sin perfil, suspendido o inválido: valores de Debian)."""
    if _leer_json(ARRANQUES).get("suspendido"):
        return None
    for f in PERFILES:
        if os.path.isfile(f):
            try:
                return P.cargar(f)
            except (P.PerfilInvalido, OSError, ValueError):
                continue
    return None


def arranque_inicio() -> int:
    """Al empezar cada arranque (servicio temprano): un arranque más sin confirmar. Con 2 seguidos, se suspende."""
    d = _leer_json(ARRANQUES)
    d["pendientes"] = int(d.get("pendientes", 0)) + 1
    if d["pendientes"] >= 2 and not d.get("suspendido"):
        d["suspendido"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    _guardar_json(ARRANQUES, d)
    return d["pendientes"]


def arranque_ok() -> None:
    """Llegó al escritorio y siguió estable (fase 2): el contador vuelve a cero."""
    d = _leer_json(ARRANQUES)
    d["pendientes"] = 0
    _guardar_json(ARRANQUES, d)


ALERTAS = os.environ.get("ESCUDO_ALERTS", "/run/escudo/alertas.jsonl")


def avisar_suspendido(alertas: str | None = None) -> bool:
    """Si el perfil del equipo quedó suspendido (dos arranques seguidos sin confirmar), avisa UNA vez, con botón
    para volver a intentarlo. Antes pasaba en silencio y el equipo seguía para siempre con los valores de Debian."""
    d = _leer_json(ARRANQUES)
    if not d.get("suspendido") or d.get("avisado") == d["suspendido"]:
        return False
    try:
        os.makedirs(os.path.dirname(alertas or ALERTAS), exist_ok=True)
        with open(alertas or ALERTAS, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "kind": "sistema", "severity": "low", "boton": "reanudar_perfil",
                                 "detail": "El equipo no terminó de arrancar dos veces seguidas, así que se dejaron de "
                                           "usar los ajustes de rendimiento de este equipo (por si eran la causa).",
                                 "action": "Si fue un apagón o lo apagaste tú, puedes volver a usarlos."},
                                ensure_ascii=False) + "\n")
    except OSError:
        return False
    d["avisado"] = d["suspendido"]
    _guardar_json(ARRANQUES, d)
    return True


def reanudar_perfil() -> None:
    """Tras publicar un perfil nuevo (o a mano): se vuelve a intentar."""
    _guardar_json(ARRANQUES, {"pendientes": 0})
