"""Modelos especialistas: varios modelos a la vez, cada uno para la tarea que mejor hace.

El modelo principal (el del chat) delega con la herramienta `especialista` en modelos pequeños y
muy especializados, que actúan como herramientas suyas. Dos tipos:

* llm: un modelo de chat (GGUF descargado por TISMA Code, servido con llama.cpp en su propio puerto,
  o un motor local ya en marcha). Ej.: un modelo de 0,5 B afinado para SQL o para regex.
* programa: un modelo que NO es de chat (p. ej. TRM, Tiny Recursive Model, para sudokus, laberintos o
  ARC) o cualquier solucionador. Se ejecuta en bubblewrap, sin red y sin ver tu carpeta personal; recibe
  la entrada por stdin y responde por stdout (texto o JSON {"salida": ...}).

Cada especialista puede declarar un verificador; su resultado se comprueba antes de dárselo al
modelo principal (rigor: un especialista también se equivoca). Verificadores incluidos: «sudoku»,
«json»; o un comando propio (código de salida 0 = válido).

Además, `roles` asigna modelos a tareas internas del arnés (p. ej. «hope»: el crítico y el consejo
de Hope usan ese modelo en lugar del principal).

La configuración solo se lee de ~/.config/tisma-code/especialistas.json (nunca del proyecto: un
repositorio no puede declarar comandos que se ejecuten). Ejemplo:
{
  "especialistas": [
    {"nombre": "sudoku", "tipo": "programa", "descripcion": "Resuelve sudokus 9x9 (TRM, 7 M parámetros)",
     "comando": ["python3", "run.py"], "carpeta": "~/TISMA/Especialistas/trm-sudoku",
     "verificador": "sudoku", "disparadores": ["sudoku"], "tiempo": 120},
    {"nombre": "sql", "tipo": "llm", "descripcion": "Escribe consultas SQL",
     "gguf": "~/.local/share/tisma-code/models/sql-0.5b-q8.gguf", "sistema": "Responde solo con SQL."}
  ],
  "roles": {"hope": "sql"}
}
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

CONFIG = Path.home() / ".config/tisma-code/especialistas.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MAX_SERVERS = 1          # modelos de chat extra cargados a la vez (RAM: equipos de 8-16 GB)
IDLE_UNLOAD = 180        # s sin uso → el modelo se descarga de la memoria
MAX_IO = 200_000


class SpecialistError(RuntimeError):
    pass


@dataclass
class Specialist:
    nombre: str
    tipo: str
    descripcion: str
    comando: list[str] = field(default_factory=list)
    carpeta: str = ""
    gguf: str = ""
    endpoint: str = ""
    modelo: str = ""
    sistema: str = ""
    temperatura: float = 0.1
    verificador: str | list[str] = ""
    disparadores: list[str] = field(default_factory=list)
    tiempo: int = 120


def _expand(p: str) -> str:
    return str(Path(os.path.expanduser(p)).resolve()) if p else ""


def load(path: Path = CONFIG) -> tuple[dict[str, Specialist], dict[str, str]]:
    try:
        st = path.lstat()
    except OSError:
        return {}, {}
    # Solo un archivo normal del propio usuario y no escribible por otros.
    if not path.is_file() or path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise SpecialistError(f"{path}: debe ser un archivo tuyo y no escribible por otros usuarios")
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Specialist] = {}
    for raw in data.get("especialistas", []):
        s = Specialist(**{k: v for k, v in raw.items() if k in Specialist.__dataclass_fields__})
        if not NAME_RE.match(s.nombre):
            raise SpecialistError(f"nombre de especialista no válido: {s.nombre!r}")
        if s.tipo not in ("llm", "programa"):
            raise SpecialistError(f"{s.nombre}: tipo debe ser «llm» o «programa»")
        if s.tipo == "programa" and not (s.comando and isinstance(s.comando, list)):
            raise SpecialistError(f"{s.nombre}: falta «comando» (lista de argumentos)")
        if s.tipo == "llm" and not (s.gguf or s.endpoint):
            raise SpecialistError(f"{s.nombre}: indica «gguf» o «endpoint»")
        if s.endpoint:
            from urllib.parse import urlparse
            u = urlparse(s.endpoint)
            if u.scheme != "http" or u.username or u.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise SpecialistError(f"{s.nombre}: el endpoint debe ser local (http://127.0.0.1:…): TISMA Code "
                                      "no envía tus datos a servidores externos")
        s.carpeta, s.gguf = _expand(s.carpeta), _expand(s.gguf)
        s.tiempo = max(5, min(int(s.tiempo), 1800))
        out[s.nombre] = s
    roles = {k: v for k, v in (data.get("roles") or {}).items() if v in out}
    return out, roles


# ---------------------------------------------------------------- verificadores
def _grid(text: str) -> list[int] | None:
    digits = [int(c) for c in re.sub(r"[.\-_]", "0", text) if c.isdigit()]
    return digits if len(digits) == 81 else None


def verify_sudoku(entrada: str, salida: str) -> tuple[bool, str]:
    puzzle, sol = _grid(entrada), _grid(salida)
    if puzzle is None or sol is None:
        return False, "la entrada o la salida no son una cuadrícula de 81 casillas"
    if any(p and p != s for p, s in zip(puzzle, sol)):
        return False, "la solución cambia números de la entrada"
    full = set(range(1, 10))
    for i in range(9):
        row = sol[9 * i: 9 * i + 9]
        col = sol[i::9]
        box = [sol[9 * (3 * (i // 3) + r) + 3 * (i % 3) + c] for r in range(3) for c in range(3)]
        for kind, cells in (("fila", row), ("columna", col), ("caja", box)):
            if set(cells) != full:
                return False, f"{kind} {i + 1} no contiene 1-9 exactamente una vez"
    return True, "sudoku válido: filas, columnas y cajas correctas y respeta la entrada"


def verify_json(entrada: str, salida: str) -> tuple[bool, str]:
    try:
        json.loads(salida)
        return True, "JSON válido"
    except ValueError as e:
        return False, f"JSON no válido: {e}"


BUILTIN_VERIFIERS = {"sudoku": verify_sudoku, "json": verify_json}


# ---------------------------------------------------------------- ejecución
def jail_argv(s: Specialist, argv: list[str]) -> list[str]:
    from . import models
    bwrap = shutil.which("bwrap")
    if not bwrap:
        raise SpecialistError("bubblewrap no está instalado: los especialistas no se ejecutan sin aislamiento")
    home = str(Path.home())
    base = [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--tmpfs", "/run", "--tmpfs", "/var/tmp", "--tmpfs", home]
    if s.carpeta:
        base += ["--ro-bind", s.carpeta, s.carpeta, "--chdir", s.carpeta]
    # «--dev /dev» crea un /dev mínimo: la GPU (/dev/dri, /dev/kfd, NVIDIA) se monta aparte para los
    # especialistas con aceleración
    return base + models.gpu_binds() + ["--unshare-all", "--die-with-parent", "--new-session", "--cap-drop", "ALL", "--clearenv",
                   "--setenv", "PATH", "/usr/local/bin:/usr/bin:/bin", "--setenv", "HOME", home,
                   "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1", "--", *argv]


def run_program(s: Specialist, entrada: str, jail: bool = True) -> str:
    argv = jail_argv(s, s.comando) if jail else s.comando
    try:
        r = subprocess.run(argv, input=entrada[:MAX_IO], capture_output=True, text=True, timeout=s.tiempo,
                           cwd=s.carpeta or None, errors="replace")
    except subprocess.TimeoutExpired:
        raise SpecialistError(f"«{s.nombre}» superó {s.tiempo} s")
    except OSError as e:
        raise SpecialistError(f"«{s.nombre}» no se pudo ejecutar: {e}")
    if r.returncode != 0:
        raise SpecialistError(f"«{s.nombre}» terminó con código {r.returncode}: {(r.stderr or '')[-800:]}")
    out = (r.stdout or "")[:MAX_IO].strip()
    try:
        data = json.loads(out)
        if isinstance(data, dict) and "salida" in data:
            v = data["salida"]
            return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    except ValueError:
        pass
    return out


CATALOG = Path(__file__).with_name("catalogo_especialistas.json")


def catalog() -> dict:
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def install(entry_id: str, path: Path = CONFIG) -> tuple[dict, str]:
    """Añade un especialista del catálogo a la configuración del usuario. Devuelve la entrada del catálogo y
    la ruta donde quedará el GGUF (la descarga la hace models.Downloads, verificando el SHA-256)."""
    from . import models
    entry = next((e for e in catalog()["instalables"] if e["id"] == entry_id), None)
    if entry is None:
        raise SpecialistError(f"«{entry_id}» no está en el catálogo")
    gguf = models.MODELS_DIR / entry["repo"].replace("/", "__") / entry["archivo"]
    data = {"especialistas": [], "roles": {}}
    if path.exists():
        load(path)                                  # valida propietario y permisos antes de reescribir
        data.update(json.loads(path.read_text(encoding="utf-8")))
    spec = {"nombre": entry["id"], "tipo": "llm", "descripcion": entry["descripcion"], "gguf": str(gguf),
            "sistema": entry.get("sistema", ""), "disparadores": entry.get("disparadores", [])}
    if entry.get("verificador"):
        spec["verificador"] = entry["verificador"]
    data["especialistas"] = [e for e in data["especialistas"] if e.get("nombre") != entry["id"]] + [spec]
    if entry.get("rol"):
        data.setdefault("roles", {})[entry["rol"]] = entry["id"]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return entry, str(gguf)


class Pool:
    """Especialistas configurados + servidores llama.cpp bajo demanda (como máximo MAX_SERVERS)."""

    def __init__(self, llm, hw: dict | None = None, path: Path = CONFIG, jail: bool = True):
        self.llm, self.hw, self.jail = llm, hw or {}, jail
        self.specs, self.roles = load(path)
        self.stats: dict[str, list[int]] = {n: [0, 0] for n in self.specs}    # llamadas, verificadas
        self._servers: dict[str, object] = {}
        self._last_use: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._reaper: threading.Thread | None = None

    def describe(self) -> str:
        """Una sola línea en las instrucciones: el catálogo se consulta bajo demanda (especialista_buscar),
        igual que las herramientas de REA o Craft Anything, para no gastar contexto."""
        if not self.specs:
            return ""
        return (f"\n\nHay {len(self.specs)} modelo(s) especialista(s) más rápidos y precisos en tareas concretas. "
                "Si una subtarea encaja, búscalo con especialista_buscar y delega con especialista "
                "(entrada completa y autocontenida).")

    def _line(self, s: Specialist) -> str:
        ver = (", verificado: " + (s.verificador if isinstance(s.verificador, str) else "comando")) if s.verificador else ""
        n, ok = self.stats.get(s.nombre, [0, 0])
        hist = f" · {ok}/{n} verificadas" if n else ""
        return f"- {s.nombre} ({'modelo' if s.tipo == 'llm' else 'programa'}{ver}{hist}): {s.descripcion}"

    def search(self, query: str, limit: int = 8) -> str:
        words = re.findall(r"\w{3,}", query.lower())
        scored = []
        for s in self.specs.values():
            text = " ".join([s.nombre, s.descripcion, *s.disparadores]).lower()
            score = sum(w in text for w in words) if words else 1
            if score:
                scored.append((score, s.nombre, s))
        scored.sort(key=lambda x: (-x[0], x[1]))
        out = [self._line(s) for _, _, s in scored[:limit]]
        return "\n".join(out) if out else "Ningún especialista encaja: hazlo tú con tus herramientas."

    def hint(self, text: str) -> str:
        low = text.lower()
        hits = [s.nombre for s in self.specs.values() if any(d.lower() in low for d in s.disparadores)]
        return f"\n\n[Pista: el especialista {', '.join(hits)} cubre esta tarea]" if hits else ""

    def _endpoint(self, s: Specialist) -> tuple[str, str, object]:
        """endpoint, modelo y cliente para un especialista llm."""
        import functools

        from .agent import chat_completion
        if s.endpoint:
            return s.endpoint, s.modelo or "local", self.llm
        from . import models
        with self._lock:
            srv = self._servers.get(s.nombre)
            if srv is None:
                while len(self._servers) >= MAX_SERVERS:          # libera RAM: descarga el más antiguo
                    old = next(iter(self._servers))
                    self._servers.pop(old).stop()
                if not Path(s.gguf).exists():
                    raise SpecialistError(f"el modelo de «{s.nombre}» aún no está descargado")
                srv = models.LocalServer()
                srv.start(s.gguf, self.hw, ctx=8192)
                self._servers[s.nombre] = srv
            else:                                                  # mueve al final (más reciente)
                self._servers[s.nombre] = self._servers.pop(s.nombre)
            self._last_use[s.nombre] = time.monotonic()
            if self._reaper is None:
                self._reaper = threading.Thread(target=self._reap, daemon=True)
                self._reaper.start()
        ep = f"http://127.0.0.1:{srv.port}/v1"
        return ep, srv.model, functools.partial(chat_completion, api_key=srv.api_key)

    def _reap(self) -> None:
        """Descarga de la memoria los modelos que llevan IDLE_UNLOAD s sin usarse."""
        while not self._stop.wait(15):
            self.unload_idle()

    def unload_idle(self, idle: float = IDLE_UNLOAD) -> list[str]:
        now, gone = time.monotonic(), []
        with self._lock:
            for name in list(self._servers):
                if now - self._last_use.get(name, 0) >= idle:
                    self._servers.pop(name).stop()
                    gone.append(name)
        return gone

    def chat(self, nombre: str, messages: list) -> str:
        """Para roles internos (p. ej. el crítico de Hope)."""
        s = self.specs[nombre]
        ep, model, client = self._endpoint(s)
        msg, _ = client(ep, model, messages, None)
        self._last_use[nombre] = time.monotonic()
        return msg.get("content") or ""

    def verify(self, s: Specialist, entrada: str, salida: str) -> tuple[bool | None, str]:
        v = s.verificador
        if not v:
            return None, "sin verificador: trátalo como dato no comprobado"
        if isinstance(v, str):
            if v not in BUILTIN_VERIFIERS:
                return False, f"verificador desconocido «{v}»"
            return BUILTIN_VERIFIERS[v](entrada, salida)
        checker = Specialist(nombre=s.nombre + "-verificador", tipo="programa", descripcion="", comando=list(v),
                             carpeta=s.carpeta, tiempo=min(s.tiempo, 120))
        try:
            run_program(checker, json.dumps({"entrada": entrada, "salida": salida}), self.jail)
            return True, "el verificador del especialista aceptó la salida"
        except SpecialistError as e:
            return False, f"el verificador rechazó la salida: {e}"

    def ask(self, nombre: str, entrada: str) -> str:
        s = self.specs.get(nombre)
        if s is None:
            raise SpecialistError(f"no hay especialista «{nombre}». Disponibles: {', '.join(self.specs) or 'ninguno'}")
        if s.tipo == "programa":
            salida = run_program(s, entrada, self.jail)
        else:
            msgs = ([{"role": "system", "content": s.sistema}] if s.sistema else []) + \
                [{"role": "user", "content": entrada[:MAX_IO]}]
            salida = self.chat(nombre, msgs).strip()
        ok, why = self.verify(s, entrada, salida)
        self.stats[nombre][0] += 1
        if ok:
            self.stats[nombre][1] += 1
        estado = {True: "VERIFICADO", False: "RECHAZADO por el verificador", None: "NO verificado"}[ok]
        return f"[{s.nombre} · {estado}: {why}]\n{salida}"

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            for srv in self._servers.values():
                srv.stop()
            self._servers.clear()


SCHEMAS = [
    {"type": "function", "function": {
        "name": "especialista_buscar", "description": "Busca qué modelo especialista cubre una subtarea "
        "(p. ej. «sudoku», «sql», «transcribir audio»). Solo se cargan cuando se usan.",
        "parameters": {"type": "object", "required": ["consulta"], "properties": {"consulta": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "especialista", "description": "Delega una subtarea en un modelo especialista pequeño (ver la lista "
        "con especialista_buscar). Se carga solo al usarlo. Devuelve su respuesta y si su verificador la aceptó.",
        "parameters": {"type": "object", "required": ["nombre", "entrada"], "properties": {
            "nombre": {"type": "string"}, "entrada": {"type": "string",
                                                      "description": "Entrada completa y autocontenida"}}}}},
]
