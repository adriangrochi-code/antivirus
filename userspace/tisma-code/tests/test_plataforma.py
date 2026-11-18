"""plataforma.py: Linux sin cambios y, en cualquier sistema, la lógica de las reglas de seguridad de Windows
(con ES_WINDOWS simulado y Win32 sustituido). Las pruebas con las API reales de Windows están en test_windows.py."""
import ctypes
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_code import agent, exactas, models, plataforma, tools  # noqa: E402
from tisma_code.base import ToolError  # noqa: E402

RAIZ = Path(__file__).resolve().parents[1] / "tisma_code"
solo_linux = pytest.mark.skipif(sys.platform == "win32", reason="comprueba que Linux no cambia")


# ---------------------------------------------------------------- Linux: exactamente como antes
@solo_linux
def test_linux_rutas_senales_y_capacidades_de_siempre():
    p = plataforma
    assert not p.ES_WINDOWS
    assert p.DIR_DATOS == Path.home() / ".local/share/tisma-code"
    assert p.DIR_CONFIG == Path.home() / ".config/tisma-code"
    if "TISMA_MODELS_DIR" not in os.environ:
        assert models.MODELS_DIR == Path.home() / ".local/share/tisma-code/models"
    from tisma_code import especialistas, memory, server
    assert server.CONFIG == Path.home() / ".config/tisma-code/config.json"
    assert especialistas.CONFIG == Path.home() / ".config/tisma-code/especialistas.json"
    assert memory.DATA == Path.home() / ".local/share/tisma-code"
    assert p.SENALES_SALIDA == (signal.SIGTERM, signal.SIGHUP)
    assert p.capacidades() == {"aislamiento": True, "sin_apuro": True, "componentes": True, "cada_vez": []}
    assert not p.aprobar_cada_vez("run_command") and not p.aprobar_cada_vez("verify")
    esquemas = list(tools.TOOL_SCHEMAS)
    assert p.filtrar_esquemas(esquemas) is esquemas
    assert {"rea", "craft", "solidcraft", "hope", "ingenieria"} <= tools.TOOL_NAMES


@solo_linux
def test_linux_archivo_privado_igual_que_antes(tmp_path):
    f = tmp_path / "a.json"
    f.write_text("{}")
    f.chmod(0o600)
    assert plataforma.es_archivo_privado(f.lstat())
    f.chmod(0o666)
    assert not plataforma.es_archivo_privado(f.lstat())


def test_popen_en_job_no_existe_fuera_de_windows(monkeypatch):
    monkeypatch.setattr(plataforma, "ES_WINDOWS", False)
    with pytest.raises(RuntimeError):
        plataforma.popen_en_job(["x"], plataforma.Limites())


# ---------------------------------------------------------------- Win32: estructuras y límites
def test_carpeta_de_datos_en_localappdata():
    assert plataforma._dir_datos_windows({"LOCALAPPDATA": "C:\\Users\\ana\\AppData\\Local"}) == \
        Path("C:\\Users\\ana\\AppData\\Local") / "TISMA Code"


@pytest.mark.skipif(ctypes.sizeof(ctypes.c_void_p) != 8, reason="tamaños del SDK de 64 bits")
def test_estructuras_miden_lo_del_sdk_x64():
    s = ctypes.sizeof
    assert s(plataforma.JOBOBJECT_BASIC_LIMIT_INFORMATION) == 64
    assert s(plataforma.IO_COUNTERS) == 48
    assert s(plataforma.JOBOBJECT_EXTENDED_LIMIT_INFORMATION) == 144
    assert s(plataforma.MEMORYSTATUSEX) == 64
    assert s(plataforma.THREADENTRY32) == 28
    assert s(plataforma.PROCESSENTRY32W) == 568
    assert plataforma.JOBOBJECT_EXTENDED_LIMIT_INFORMATION.ProcessMemoryLimit.offset == 112


def test_limites_del_motor_trabajador_y_comandos():
    p = plataforma
    info, ui = p.valores_job(p.Limites(memoria=16 << 30, procesos=1, ui=p.UI_MOTOR))
    f = info.BasicLimitInformation.LimitFlags
    assert f & p.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE and f & p.JOB_OBJECT_LIMIT_ACTIVE_PROCESS
    assert f & p.JOB_OBJECT_LIMIT_PROCESS_MEMORY and info.ProcessMemoryLimit == 16 << 30
    assert info.BasicLimitInformation.ActiveProcessLimit == 1 and ui == p.UI_MOTOR
    assert ui & p.UILIMIT_READCLIPBOARD and ui & p.UILIMIT_EXITWINDOWS
    # Trabajador de cálculo: 3 GiB, 60 s de CPU, un solo proceso, interfaz estricta
    info, ui = p.valores_job(p.Limites(memoria=p.TRABAJADOR_MEMORIA, cpu_s=exactas.TIEMPO, procesos=1,
                                       ui=p.UI_ESTRICTA))
    b = info.BasicLimitInformation
    assert b.LimitFlags & p.JOB_OBJECT_LIMIT_PROCESS_TIME and b.PerProcessUserTimeLimit == 60 * 10_000_000
    assert info.ProcessMemoryLimit == 3 << 30 and ui & p.UILIMIT_HANDLES
    # Sin límites pedidos: igual muere con la app
    info, _ = p.valores_job(p.Limites())
    assert info.BasicLimitInformation.LimitFlags == (p.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE |
                                                    p.JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION)


def test_comandos_con_tiempo_y_memoria_de_todo_el_arbol(monkeypatch):
    visto = {}
    monkeypatch.setattr(plataforma, "ram_total_mb", lambda: 16384)
    monkeypatch.setattr(plataforma, "ejecutar_en_job", lambda argv, lim, **kw: visto.update(argv=argv, lim=lim, **kw))
    plataforma.ejecutar_comando('echo "hola"', Path("proyecto"), 120)
    lim = visto["lim"]
    assert lim.memoria_job == 8 << 30 and lim.cpu_job_s >= 120 and lim.procesos == 64
    assert visto["timeout"] == 120 and visto["cwd"] == "proyecto"
    assert visto["argv"].endswith('cmd.exe" /d /s /c "echo "hola""')
    assert "PYTHONDONTWRITEBYTECODE" in visto["env"]


def test_entorno_minimo_sin_secretos(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "secreto")
    monkeypatch.setenv("OPENAI_API_KEY", "secreto")
    monkeypatch.setenv("PATH", "/bin")
    env = plataforma.entorno_minimo({"X": "1"})
    assert "secreto" not in env.values() and env["PATH"] == "/bin" and env["X"] == "1"
    env = plataforma.entorno_minimo(claves=plataforma.CLAVES_COMANDO)
    assert "GITHUB_TOKEN" not in env and "OPENAI_API_KEY" not in env


def test_enchufado():
    st = plataforma.SYSTEM_POWER_STATUS
    assert ctypes.sizeof(st) == 12
    assert plataforma.enchufado_segun(st(ACLineStatus=1, BatteryFlag=8))
    assert not plataforma.enchufado_segun(st(ACLineStatus=0, BatteryFlag=1))         # a batería
    assert plataforma.enchufado_segun(st(ACLineStatus=255, BatteryFlag=128))         # sin batería


# ---------------------------------------------------------------- puertos y procesos
def _tabla4(filas):
    raw = struct.pack("<I", len(filas))
    for ip, port, pid in filas:
        addr = struct.unpack("<I", socket.inet_aton(ip))[0]
        p = struct.unpack("<I", struct.pack(">H", port) + b"\0\0")[0]
        raw += struct.pack("<6I", 2, addr, p, 0, 0, pid)
    return raw


def _tabla6(filas):
    raw = struct.pack("<I", len(filas))
    for ip, port, pid in filas:
        p = struct.unpack("<I", struct.pack(">H", port) + b"\0\0")[0]
        raw += socket.inet_pton(socket.AF_INET6, ip) + struct.pack("<II", 0, p) + b"\0" * 16 + \
            struct.pack("<IIII", 0, 0, 2, pid)
    return raw


def test_tabla_tcp_ipv4_e_ipv6():
    assert plataforma.filas_tcp4(_tabla4([("127.0.0.1", 41234, 777), ("0.0.0.0", 445, 4)])) == \
        [("127.0.0.1", 41234, 777), ("0.0.0.0", 445, 4)]
    assert plataforma.filas_tcp6(_tabla6([("::1", 8080, 99)])) == [("::1", 8080, 99)]


def test_puerto_solo_es_mio_si_todo_oyente_es_propio():
    es = plataforma.puerto_es_mio
    assert es(9000, oyentes=[("127.0.0.1", 10)], propios={10, 11})
    assert not es(9000, oyentes=[], propios={10})                                   # nadie escucha
    assert not es(9000, oyentes=[("127.0.0.1", 12)], propios={10})                  # otro programa
    # Otro programa compartiendo el puerto (SO_REUSEADDR) junto al motor: no es el motor
    assert not es(9000, oyentes=[("127.0.0.1", 10), ("0.0.0.0", 66)], propios={10})


def test_hijos_ignoran_pids_reutilizados():
    padres = {10: 1, 11: 10, 12: 11, 13: 10, 50: 10}
    creado = {10: 100, 11: 110, 12: 120, 13: 130, 50: 5}.get        # 50 es más viejo que 10: su padre era otro
    assert plataforma.hijos_de(10, padres, lambda p: creado(p, 0)) == [11, 13, 12]


# ---------------------------------------------------------------- ventana de la app
def test_ventana_edge_luego_chrome_luego_predeterminado(monkeypatch, tmp_path):
    env = {"ProgramFiles(x86)": "C:\\PF86", "ProgramFiles": "C:\\PF", "LOCALAPPDATA": "C:\\U\\L"}
    monkeypatch.setattr(plataforma.os, "environ", env)
    cands = plataforma.candidatos_navegador()
    assert cands[0].endswith(os.path.join("Microsoft", "Edge", "Application", "msedge.exe"))
    assert cands[-1].endswith("chrome.exe")
    lanzados = []
    url = "http://127.0.0.1:5000/?t=uno"
    plataforma.abrir_ventana_app(url, tmp_path / "perfil", existe=lambda p: p.endswith("chrome.exe"),
                                 lanzar=lambda argv, **k: lanzados.append(argv) or "proc")
    argv = lanzados[0]
    assert argv[0].endswith("chrome.exe") and f"--app={url}" in argv and \
        f"--user-data-dir={tmp_path / 'perfil'}" in argv
    abiertos = []
    monkeypatch.setattr(plataforma.os, "startfile", lambda u: abiertos.append(u), raising=False)
    assert plataforma.abrir_ventana_app(url, tmp_path / "p", existe=lambda p: False) is None
    assert abiertos == [url]


# ---------------------------------------------------------------- reglas de seguridad (ES_WINDOWS simulado)
@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(plataforma, "ES_WINDOWS", True)
    monkeypatch.setattr(plataforma, "ejecutar_comando", lambda *a, **k: pytest.fail("se ejecutó sin permiso"))
    return monkeypatch


def test_comandos_desactivados_por_defecto(win, tmp_path):
    ws = tools.Workspace(str(tmp_path))
    assert ws.run_command("dir") == plataforma.MSG_COMANDOS
    assert "actívalos en Ajustes" in tools.execute(ws, "run_command", {"command": "del /q *"})
    ws.sin_aislar = True                                  # activado, pero esta orden no se aprobó
    assert ws.run_command("dir") == plataforma.MSG_COMANDOS
    with pytest.raises(ToolError, match="no se ejecutan aislados"):
        ws.sandbox_argv("true")                          # linters, git, rg: nunca sin jaula


def test_comando_activado_y_aprobado_corre_en_job_y_el_permiso_no_se_reutiliza(win, tmp_path):
    vistos = []

    def ejecutar(command, cwd, timeout):
        vistos.append((command, cwd, timeout))
        return subprocess.CompletedProcess(command, 0, "hecho\n", "")
    win.setattr(plataforma, "ejecutar_comando", ejecutar)
    ws = tools.Workspace(str(tmp_path))
    ws.sin_aislar, ws.aprobado = True, True
    assert tools.execute(ws, "run_command", {"command": "pytest -q"}).startswith("[código de salida 0]\nhecho")
    assert vistos == [("pytest -q", ws.root, 120)]
    assert ws.aprobado is False                             # la próxima orden vuelve a pedir permiso
    assert tools.execute(ws, "run_command", {"command": "pytest -q"}) == plataforma.MSG_COMANDOS
    assert "SIN aislamiento" in tools.preview(ws, "run_command", {"command": "pytest -q"})


def test_comando_que_tarda_mata_el_arbol(win, tmp_path):
    def ejecutar(command, cwd, timeout):
        raise subprocess.TimeoutExpired(command, timeout)
    win.setattr(plataforma, "ejecutar_comando", ejecutar)
    ws = tools.Workspace(str(tmp_path))
    ws.sin_aislar = ws.aprobado = True
    assert ws.run_command("ping -t x", timeout=5) == "Tiempo agotado (5 s)"


def _sesion(tmp_path_factory, project):
    return agent.Session(str(project), "http://x/v1", "m", llm=lambda *a: ({"content": "ok"}, {}),
                         kb_dir=tmp_path_factory.mktemp("kb"))


def _responder(s, allow, always):
    def r():
        for _ in range(200):
            if s._pending:
                s.resolve_approval(next(iter(s._pending)), allow, always)
                return
            time.sleep(0.01)
    t = threading.Thread(target=r)
    t.start()
    return t


def test_permiso_cada_vez_nunca_siempre(win, tmp_path, tmp_path_factory):
    s = _sesion(tmp_path_factory, tmp_path)
    # Desactivados: no se pregunta nada (al ejecutar se devuelve el aviso) y no queda aprobado
    assert s._ask("run_command", {"command": "dir"}, "") is True and s.ws.aprobado is False
    s.ws.sin_aislar = True
    t = _responder(s, True, True)                            # «Sí, y no volver a preguntar»
    assert s._ask("run_command", {"command": "dir"}, "") is True
    t.join()
    assert s.ws.aprobado is True and "run_command" not in s.always_allow
    t = _responder(s, True, True)                            # «confío en las pruebas»: tampoco se guarda
    assert s._ask("verify", {}, "")
    t.join()
    assert not s.trusted() and "verify" not in s.always_allow
    s.always_allow.add("run_command")                        # aunque algo lo hubiera añadido, se vuelve a preguntar
    t = _responder(s, False, False)
    assert s._ask("run_command", {"command": "dir"}, "") is False and s.ws.aprobado is False
    t.join()
    s.close()


def test_auto_verificar_solo_con_comandos_activados(win, tmp_path, tmp_path_factory):
    from tisma_code import server
    s = _sesion(tmp_path_factory, tmp_path)
    app = server.App.__new__(server.App)
    app.config = {}
    app.aplicar_comandos(s)
    assert s.ws.sin_aislar is False and s.auto_verify is False
    app.config = {"comandos_sin_aislar": True}
    app.aplicar_comandos(s)
    assert s.ws.sin_aislar is True and s.auto_verify is True
    s.close()


def test_herramientas_de_tisma_os_no_se_ofrecen(win):
    filtrados = {x["function"]["name"] for x in plataforma.filtrar_esquemas(list(tools.TOOL_SCHEMAS))}
    assert not {x for x in filtrados if x.startswith(("rea_", "craft", "solidcraft", "hope", "ingenieria")) or x == "rea"}
    assert not filtrados & {"consejo", "jarvis", "git", "formatear", "diagnosticos"}
    assert {"read_file", "edit_file", "run_command", "verify", "calcular"} <= filtrados


def test_motor_solo_con_job(win, monkeypatch):
    with pytest.raises(RuntimeError, match="aislado"):
        models.jail_engine(["llama-server"])              # nunca un Popen sin nada
    lanzados = []
    monkeypatch.setattr(plataforma, "lanzar_motor", lambda argv: lanzados.append(argv) or "proc")
    assert models.LocalServer._spawn(["llama-server", "--api-key", "k"]) == "proc"
    assert lanzados == [["llama-server", "--api-key", "k"]]
    monkeypatch.setattr(plataforma, "puerto_es_mio", lambda port: port == 4321)
    assert models.port_owner_is_me(4321) and not models.port_owner_is_me(80)
    cerrados = []
    monkeypatch.setattr(plataforma, "cerrar_motor", lambda proc, espera: cerrados.append(proc))
    models.cerrar_ordenado("p")
    monkeypatch.setattr(plataforma, "cerrar_motores", lambda espera: 2)
    assert cerrados == ["p"] and models.cerrar_motores_hijos() == 2


def test_motor_en_job_con_un_proceso_y_memoria_de_la_ram(win, monkeypatch, tmp_path):
    visto = {}
    monkeypatch.setattr(plataforma, "DIR_DATOS", tmp_path)
    monkeypatch.setattr(plataforma, "ram_total_mb", lambda: 8192)
    monkeypatch.setattr(plataforma, "popen_en_job", lambda argv, lim, **kw: visto.update(argv=argv, lim=lim, **kw))
    plataforma.lanzar_motor(["llama-server", "--host", "127.0.0.1"])
    assert visto["lim"].procesos == 1 and visto["lim"].memoria == 8192 << 20
    assert visto["nuevo_grupo"] and visto["motor"] and visto["stdin"] == subprocess.DEVNULL
    assert set(visto["env"]) <= {"SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "PATH", "PATHEXT", "NUMBER_OF_PROCESSORS",
                                 "PROCESSOR_ARCHITECTURE", "OS", "TEMP", "TMP"}


def test_calculo_exacto_en_job(win, monkeypatch, tmp_path):
    vistos = []

    def trabajador(worker, raiz, pedido, tiempo):
        vistos.append((worker, raiz, json.loads(pedido), tiempo))
        return subprocess.CompletedProcess([], 0, json.dumps({"ok": True, "resultado": "3"}), "")
    monkeypatch.setattr(plataforma, "ejecutar_trabajador", trabajador)
    monkeypatch.setattr(exactas, "argv", lambda raiz: pytest.fail("bubblewrap no existe aquí"))
    assert exactas.run(tmp_path, "calcular", {"expresion": "1+2"}) == ("3", True)
    assert vistos[0][0] == exactas.WORKER and vistos[0][3] == exactas.TIEMPO

    def falla(*a):
        raise OSError("sin Job")
    monkeypatch.setattr(plataforma, "ejecutar_trabajador", falla)
    texto, ok = exactas.run(tmp_path, "calcular", {"expresion": "1+2"})
    assert not ok and "No se pudo lanzar" in texto


def test_trabajador_sin_job_corre_sin_eval(win, monkeypatch, tmp_path):
    """Si el Job no se puede crear, el trabajador se lanza igual (sus herramientas propias no usan eval) y él mismo
    rechaza la vía de sympy."""
    monkeypatch.setattr(plataforma, "DIR_DATOS", tmp_path)
    monkeypatch.setattr(plataforma, "crear_job", lambda lim: (_ for _ in ()).throw(OSError("no")))
    monkeypatch.setattr(plataforma, "ejecutar_en_job", lambda *a, **k: pytest.fail("no hay Job"))
    monkeypatch.setattr(plataforma, "_hay_consola", lambda: True)
    visto = {}

    def run(argv, **kw):
        visto.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, "{}", "")
    monkeypatch.setattr(plataforma.subprocess, "run", run)
    plataforma.ejecutar_trabajador(exactas.WORKER, tmp_path, "{}", 60)
    assert visto["argv"][-2:] == ["-I", str(exactas.WORKER)] and "-X" in visto["argv"]
    assert "PATH" not in visto["env"] and visto["timeout"] == 70


def test_sympy_solo_dentro_del_job(monkeypatch):
    sympy = pytest.importorskip("sympy")
    from tisma_code import exactas_worker as w
    real = w.jail_activo
    monkeypatch.setattr(w, "ES_WINDOWS", True)
    monkeypatch.setattr(w, "jail_activo", lambda: False)
    with pytest.raises(w.Fallo, match="aislado"):
        w._sympy_expr("x + 1", sympy)
    monkeypatch.setattr(w, "jail_activo", lambda: True)
    assert str(w._sympy_expr("x + 1", sympy)) == "x + 1"
    with pytest.raises(w.Fallo):
        w._sympy_expr("__import__('os')", sympy)          # el filtro de la entrada sigue ahí
    if sys.platform != "win32":
        assert real() is False                   # fuera de Windows no hay Job: nunca «aislado»


def test_reducir_en_job(win, monkeypatch, tmp_path):
    from tisma_code import reducir
    with pytest.raises(reducir.ReducirError, match="aislada"):
        reducir.jaula(["llama-quantize"], tmp_path)
    cerrados = []

    class P:
        def __init__(self):
            self.stdout = __import__("io").BytesIO(b"[ 1/ 2] blk.0\n[ 2/ 2] blk.1\n")

        def wait(self):
            return 0

        def poll(self):
            return 0
    monkeypatch.setattr(plataforma, "lanzar_herramienta", lambda argv, trabajo: P())
    monkeypatch.setattr(plataforma, "cerrar_job", lambda p: cerrados.append(p))
    assert "blk.1" in reducir.ejecutar(["llama-quantize", "a", "b"], tmp_path)
    assert len(cerrados) == 1


def test_sin_herramientas_auxiliares_ni_mlock(win):
    from tisma_code import devtools, motor
    assert not devtools.have("python") and not devtools.have("git")
    assert motor.mlock_args(100, {"ram_mb": 16000}) == []


def test_carpetas_prohibidas(win, monkeypatch, tmp_path):
    casa = tmp_path / "casa"
    (casa / "AppData" / "Roaming").mkdir(parents=True)
    (casa / "proyecto").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: casa))
    monkeypatch.setattr(plataforma, "DIR_DATOS", tmp_path / "datos")
    for mala in (casa / "AppData", casa / "AppData" / "Roaming", Path(tmp_path.anchor)):
        with pytest.raises(ToolError):
            tools.Workspace(str(mala))
    assert tools.Workspace(str(casa / "proyecto")).root == (casa / "proyecto").resolve()


def test_servidor_sin_componentes_ni_sin_apuro(win, monkeypatch):
    from tisma_code import server
    app = server.App.__new__(server.App)
    app.instalando = {}
    monkeypatch.setattr(server.subprocess, "Popen", lambda *a, **k: pytest.fail("pkexec no existe aquí"))
    assert app.componentes() == {}
    assert app.instalar_componente("rea") == {"ok": False, "error": "No disponible en este equipo"}
    assert plataforma.capacidades() == {"aislamiento": False, "sin_apuro": False, "componentes": False,
                                        "cada_vez": ["run_command", "verify"]}


def test_ventana_cerrada_cierra_la_app(monkeypatch):
    from tisma_code import server
    cerrado = threading.Event()

    class Httpd:
        def shutdown(self):
            cerrado.set()
    app = server.App.__new__(server.App)
    app.boot_used = threading.Event()
    app.ultimo_contacto = 0
    monkeypatch.setattr(server, "APP", app, raising=False)
    reloj = lambda: server.VENTANA_SIN_CONTACTO_S + 1  # noqa: E731
    assert not server.vigilar_ventana(Httpd(), reloj=reloj, una_vez=True)     # la página aún no se abrió
    app.boot_used.set()
    assert server.vigilar_ventana(Httpd(), reloj=reloj, una_vez=True) and cerrado.wait(2)


def test_textos_de_la_interfaz_no_nombran_el_sistema():
    html = (RAIZ / "static" / "index.html").read_text(encoding="utf-8")
    assert "Windows" not in html and "aj-comandos" in html
    for t in (plataforma.MSG_COMANDOS, plataforma.MSG_NO_DISPONIBLE):
        assert "Windows" not in t and "este equipo" in t


def test_ajustes_comandos_solo_booleano():
    src = (RAIZ / "server.py").read_text(encoding="utf-8")
    assert 'if isinstance(b.get("comandos_sin_aislar"), bool):' in src


def test_abrir_sin_enlaces_sin_o_nofollow(tmp_path, monkeypatch):
    """En Windows no existe os.O_NOFOLLOW («module 'os' has no attribute 'O_NOFOLLOW'» al instalar especialistas):
    se escribe igual, pero nunca a través de un enlace."""
    import os
    from tisma_code import plataforma
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    f = tmp_path / "a.json"
    fd = plataforma.abrir_sin_enlaces(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, b"ok")
    os.close(fd)
    assert f.read_bytes() == b"ok"
    (tmp_path / "enlace").symlink_to(f)
    with pytest.raises(OSError):
        plataforma.abrir_sin_enlaces(tmp_path / "enlace", os.O_WRONLY | os.O_TRUNC, 0o600)
    otra = tmp_path / "otra"
    otra.mkdir()
    (tmp_path / "carpeta-enlace").symlink_to(otra, target_is_directory=True)
    with pytest.raises(OSError):
        plataforma.abrir_sin_enlaces(tmp_path / "carpeta-enlace" / "x", os.O_WRONLY | os.O_CREAT, 0o600)
