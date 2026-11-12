"""USB que se hacen pasar por teclado (BadUSB, «Rubber Ducky», cables trucados): se detectan por CÓMO escriben.

Un dispositivo así se anuncia como teclado y, apenas se conecta, «teclea» órdenes a 100-1000 teclas por segundo (abrir
una terminal, descargar algo y ejecutarlo). Una persona no pasa de ~15 teclas por segundo y nunca con un ritmo perfecto.

Cómo protege sin molestar:
1. Teclado USB NUEVO (nunca visto): sus teclas se RETIENEN los primeros segundos (los ataques escriben apenas se
   conectan). Si el ritmo es inhumano, se bloquea sin que llegue una sola letra; si es humano, se entregan tal cual.
2. Durante el primer minuto sigue pasando por Escudo (se corta en el acto si escribe como un robot).
3. Después queda «conocido» y funciona directo, con vigilancia pasiva: si alguna vez escribe de forma inhumana, se
   corta en ese momento.
4. Al bloquear: el dispositivo queda capturado (sus teclas no llegan a nada) y USBGuard lo desconecta; aviso en el
   escritorio con el botón «Era mío» (un lector de códigos de barras, por ejemplo, también escribe muy rápido).
Seguridad ante fallos: si Escudo se cae, el kernel suelta la captura y el teclado vuelve a funcionar solo.
Los ratones puros no pueden escribir: no se tocan. Un «ratón» que también expone un teclado se vigila como teclado.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
import threading
import time
from collections import deque
from pathlib import Path

log = logging.getLogger("escudo.teclados")

ESTADO = Path(os.environ.get("ESCUDO_TECLADOS", "/var/lib/escudo/teclados.json"))
GRACIA_S = 3.0              # teclas retenidas al conectar un teclado nuevo
PRUEBA_S = 60.0             # tiempo pasando por Escudo antes de quedar «conocido»
VENTANA = 16                # pulsaciones para medir velocidad
MIN_INTERVALO = 0.030       # 16 pulsaciones en menos de 16 × 30 ms (> 33 teclas/s sostenidas) = no es una persona
REGULAR_N = 20              # ritmo de robot: 20 intervalos casi idénticos…
REGULAR_CV = 0.12           # …(coeficiente de variación < 12 %)…
REGULAR_MEDIA = 0.120       # …y rápidos (> 8 teclas/s)
IDENT = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{4}(:[\w.-]{0,64})?$")


class Detector:
    """Decide con las marcas de tiempo de las pulsaciones (solo key-down): None o el motivo."""

    def __init__(self):
        self.t: deque[float] = deque(maxlen=max(VENTANA, REGULAR_N + 1))

    def pulsacion(self, t: float) -> str | None:
        self.t.append(t)
        ts = list(self.t)
        if len(ts) >= VENTANA:
            ult = ts[-VENTANA:]
            span = ult[-1] - ult[0]
            if span < (VENTANA - 1) * MIN_INTERVALO:
                return f"escribió {VENTANA} teclas en {span * 1000:.0f} ms ({(VENTANA - 1) / max(span, 1e-3):.0f} " \
                       "teclas por segundo): imposible para una persona"
        if len(ts) > REGULAR_N:
            iv = [b - a for a, b in zip(ts[-REGULAR_N - 1:], ts[-REGULAR_N:])]
            media = statistics.fmean(iv)
            if media < REGULAR_MEDIA and media > 0 and statistics.pstdev(iv) / media < REGULAR_CV:
                return f"teclea con un ritmo perfectamente regular ({1 / media:.0f} teclas por segundo, siempre igual): " \
                       "un robot, no una persona"
        return None


def identidad(vendor: int, producto: int, serie: str = "") -> str:
    serie = re.sub(r"[^\w.-]", "", serie or "")[:64]
    return f"{vendor:04x}:{producto:04x}" + (f":{serie}" if serie else "")


class Registro:
    """Teclados conocidos (pasaron la prueba) y permitidos por el usuario; persistente."""

    def __init__(self, ruta: Path | None = None):
        self.ruta = ruta or ESTADO
        try:
            d = json.loads(self.ruta.read_text())
        except (OSError, ValueError):
            d = {}
        self.conocidos: set[str] = set(d.get("conocidos", []))
        self.permitidos: set[str] = set(d.get("permitidos", []))
        self.bloqueados: set[str] = set(d.get("bloqueados", []))
        self.lock = threading.Lock()

    def guardar(self) -> None:
        with self.lock:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.ruta.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as fh:
                json.dump({"conocidos": sorted(self.conocidos), "permitidos": sorted(self.permitidos),
                           "bloqueados": sorted(self.bloqueados)}, fh)
            os.replace(tmp, self.ruta)

    def permitir(self, ident: str) -> None:
        if not IDENT.match(ident):
            raise ValueError("identificador de dispositivo inválido")
        self.permitidos.add(ident)
        self.bloqueados.discard(ident)
        self.conocidos.add(ident)
        self.guardar()


class Vigia:
    """Vigila UN teclado USB. `dispositivo` es un evdev.InputDevice (o un doble en las pruebas) y `salida` el teclado
    virtual (evdev.UInput) por el que se reenvían las teclas mientras está capturado."""

    def __init__(self, dispositivo, ident: str, nuevo: bool, registro: Registro, bloquear, avisar, salida_factory,
                 reloj=time.monotonic):
        self.d, self.ident, self.registro = dispositivo, ident, registro
        self.bloquear, self.avisar, self.salida_factory, self.reloj = bloquear, avisar, salida_factory, reloj
        self.detector = Detector()
        self.inicio = reloj()
        self.permitido = ident in registro.permitidos
        self.capturado = False
        self.salida = None
        self.retenidos: list = []
        self.gracia = nuevo and not self.permitido
        self.prueba = (nuevo or ident not in registro.conocidos) and not self.permitido
        self.bloqueado = False
        self.pulsadas: set[int] = set()

    def empezar(self) -> None:
        if self.ident in self.registro.bloqueados and not self.permitido:
            self._bloquear("ya se había bloqueado antes por escribir como un robot")
            return
        if self.prueba:
            self._capturar()

    def _capturar(self) -> None:
        try:
            self.d.grab()
            self.salida = self.salida_factory(self.d)
            self.capturado = True
        except OSError as e:
            log.warning("No se pudo capturar %s: %s (queda solo la vigilancia pasiva)", self.ident, e)

    def _soltar(self) -> None:
        if self.salida is not None:
            self._liberar_teclas()
            try:
                self.salida.close()
            except OSError:
                pass
            self.salida = None
        if self.capturado:
            try:
                self.d.ungrab()
            except OSError:
                pass
            self.capturado = False

    def _liberar_teclas(self) -> None:
        """Al cortar: suelta las teclas que quedaron «apretadas» en el teclado virtual (si no, quedarían pegadas)."""
        for code in list(self.pulsadas):
            try:
                self.salida.write(1, code, 0)
                self.salida.syn()
            except OSError:
                pass
        self.pulsadas.clear()

    def _bloquear(self, motivo: str) -> None:
        self.bloqueado = True
        self.retenidos.clear()
        if self.salida is not None:
            self._liberar_teclas()
        if not self.capturado:
            try:
                self.d.grab()                     # sus teclas ya no llegan a nada
                self.capturado = True
            except OSError:
                pass
        self.registro.bloqueados.add(self.ident)
        self.registro.guardar()
        self.bloquear(self.d)
        nombre = getattr(self.d, "name", "") or "dispositivo USB"
        self.avisar(nombre, self.ident, motivo)

    def evento(self, tipo: int, codigo: int, valor: int) -> None:
        """Un evento del teclado (EV_KEY=1: valor 1 pulsar, 0 soltar, 2 repetición)."""
        if self.bloqueado:
            return
        ahora = self.reloj()
        if tipo == 1 and valor == 1 and not self.permitido:
            motivo = self.detector.pulsacion(ahora)
            if motivo:
                self._bloquear(motivo)
                return
        if not self.capturado:
            return                                 # vigilancia pasiva: el teclado ya entrega directo
        if self.gracia and ahora - self.inicio < GRACIA_S:
            self.retenidos.append((tipo, codigo, valor))
            return
        if self.gracia:                           # terminó la gracia con ritmo humano: se entrega lo retenido
            self.gracia = False
            for ev in self.retenidos:
                self._enviar(*ev)
            self.retenidos.clear()
        self._enviar(tipo, codigo, valor)
        if self.prueba and ahora - self.inicio >= PRUEBA_S:
            self.prueba = False
            self.registro.conocidos.add(self.ident)
            self.registro.guardar()
            self._soltar()                         # ya es conocido: desde ahora entrega directo

    def _enviar(self, tipo: int, codigo: int, valor: int) -> None:
        if tipo == 1:
            (self.pulsadas.add if valor else self.pulsadas.discard)(codigo)
        try:
            self.salida.write(tipo, codigo, valor)
            if tipo == 0:
                return
            if tipo == 1:
                self.salida.syn()
        except OSError:
            pass

    def vencio_gracia(self) -> None:
        """Llamado periódicamente: entrega lo retenido si terminó la gracia sin más teclas."""
        if self.gracia and self.capturado and not self.bloqueado and self.reloj() - self.inicio >= GRACIA_S:
            self.gracia = False
            for ev in self.retenidos:
                self._enviar(*ev)
            self.retenidos.clear()


# ---------------------------------------------------------------- integración con el sistema (evdev + USBGuard)
def es_teclado_usb(dev) -> bool:
    """USB y con teclas de letras (un ratón puro no puede escribir). Nunca nuestros propios teclados virtuales."""
    if "usb" not in (getattr(dev, "phys", "") or "") or (getattr(dev, "name", "") or "").startswith("TISMA"):
        return False
    try:
        teclas = set(dev.capabilities().get(1, []))
    except OSError:
        return False
    return {30, 48, 46, 32, 18}.issubset(teclas)        # A, B, C, D, E


def puerto_usb(dev) -> str | None:
    """«1-2» del dispositivo USB al que pertenece el teclado (para USBGuard)."""
    try:
        p = Path(f"/sys/class/input/{Path(dev.path).name}/device").resolve()
    except OSError:
        return None
    for parte in [p, *p.parents]:
        if (parte / "idVendor").exists() and re.fullmatch(r"\d+-[\d.]+", parte.name):
            return parte.name
    return None


def bloquear_usbguard(dev, run=None) -> bool:
    import subprocess
    run = run or subprocess.run
    puerto = puerto_usb(dev)
    if not puerto:
        return False
    try:
        r = run(["usbguard", "list-devices"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    for ln in r.stdout.splitlines():
        m = re.match(r"^(\d+): allow .*via-port \"" + re.escape(puerto) + r"\"", ln)
        if m:
            return run(["usbguard", "block-device", m.group(1)], capture_output=True, timeout=10).returncode == 0
    return False


class Guardia:
    """Escudo: vigila los teclados USB que hay y los que se conecten (inotify en /dev/input)."""

    def __init__(self, avisar, registro: Registro | None = None):
        import evdev                                     # python3-evdev (BSD); sin él, la guardia no arranca
        self.evdev = evdev
        self.registro = registro or Registro()
        self.avisar = avisar
        self.vigias: dict[str, Vigia] = {}
        self.stop = threading.Event()

    def _ident(self, dev) -> str:
        i = dev.info
        return identidad(i.vendor, i.product, getattr(dev, "uniq", "") or "")

    def _salida(self, dev):
        return self.evdev.UInput.from_device(dev, name=f"TISMA teclado ({(dev.name or 'USB')[:40]})")

    def vigilar(self, ruta: str, nuevo: bool) -> None:
        if ruta in self.vigias:
            return
        try:
            dev = self.evdev.InputDevice(ruta)
        except OSError:
            return
        if not es_teclado_usb(dev):
            dev.close()
            return
        v = Vigia(dev, self._ident(dev), nuevo, self.registro, bloquear_usbguard, self.avisar, self._salida)
        self.vigias[ruta] = v
        v.empezar()
        threading.Thread(target=self._leer, args=(ruta, v), daemon=True, name="teclado").start()

    def _leer(self, ruta: str, v: Vigia) -> None:
        import select
        try:
            while not self.stop.is_set():
                r, _, _ = select.select([v.d.fd], [], [], 0.5)
                if not r:
                    v.vencio_gracia()
                    continue
                for ev in v.d.read():
                    v.evento(ev.type, ev.code, ev.value)
        except OSError:
            pass                                         # desconectado
        finally:
            v._soltar()
            self.vigias.pop(ruta, None)

    def start(self) -> threading.Thread:
        for ruta in self.evdev.list_devices():
            self.vigilar(ruta, nuevo=False)              # los de antes de arrancar: prueba si no son conocidos
        t = threading.Thread(target=self._nuevos, daemon=True, name="teclados-nuevos")
        t.start()
        return t

    def _nuevos(self) -> None:
        import ctypes
        import select
        libc = ctypes.CDLL(None, use_errno=True)
        fd = libc.inotify_init1(0o4000 | 0o2000000)
        if fd < 0 or libc.inotify_add_watch(fd, b"/dev/input", 0x100 | 0x80) < 0:   # IN_CREATE | IN_MOVED_TO
            log.warning("Sin inotify en /dev/input: solo se vigilan los teclados conectados al arrancar")
            return
        while not self.stop.is_set():
            r, _, _ = select.select([fd], [], [], 1.0)
            if not r:
                continue
            try:
                os.read(fd, 65536)
            except OSError:
                pass
            time.sleep(0.2)                              # udev termina de ajustar permisos y propiedades
            for ruta in self.evdev.list_devices():
                if ruta not in self.vigias:
                    self.vigilar(ruta, nuevo=True)
