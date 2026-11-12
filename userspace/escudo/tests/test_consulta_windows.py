"""Programas de Windows: Escudo los revisa a pedido (el kernel no los ejecuta: Wine los lee) y los observa con Wine."""
import json
import os
import socket
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo import consulta, detonar  # noqa: E402


def pe(path: Path, extra: bytes = b"") -> Path:
    cab = bytearray(b"MZ" + b"\0" * 62)
    cab[60:64] = (128).to_bytes(4, "little")
    cab += b"\0" * (128 - len(cab)) + b"PE\0\0" + b"\0" * 64 + extra
    path.write_bytes(bytes(cab))
    return path


def test_reconoce_programas_de_windows(tmp_path):
    assert detonar.es_windows(str(pe(tmp_path / "setup.exe")))
    (tmp_path / "falso.exe").write_bytes(b"MZ" + b"\0" * 200)          # MZ sin cabecera PE: no
    assert not detonar.es_windows(str(tmp_path / "falso.exe"))
    (tmp_path / "x.msi").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    assert detonar.es_windows(str(tmp_path / "x.msi"))
    (tmp_path / "doc.doc").write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\0" * 100)
    assert not detonar.es_windows(str(tmp_path / "doc.doc"))          # un .doc viejo no es un instalador
    (tmp_path / "s.sh").write_text("#!/bin/sh\n")
    assert not detonar.es_windows(str(tmp_path / "s.sh"))


def test_persistencia_en_el_registro_y_la_carpeta_inicio(tmp_path):
    pref = tmp_path / ".wine"
    pref.mkdir()
    # Lo que trae un prefijo nuevo de Wine (winemenubuilder) no cuenta
    (pref / "system.reg").write_text('[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\RunServices] 1\n'
                                     '"winemenubuilder"="C:\\\\windows\\\\system32\\\\winemenubuilder.exe -a -r"\n')
    assert detonar.persistencia_windows(tmp_path) == []
    (pref / "user.reg").write_text('[Software\\\\Microsoft\\\\Windows\\\\CurrentVersion\\\\Run] 1700000000\n'
                                   '"Updater"="C:\\\\users\\\\usuario\\\\AppData\\\\x.exe"\n\n[Otra] 1\n')
    assert any("Run" in m for m in detonar.persistencia_windows(tmp_path))
    inicio = pref / "drive_c/users/usuario" / detonar.WIN_STARTUP
    inicio.mkdir(parents=True)
    (inicio / "desktop.ini").write_text("")
    (pref / "user.reg").unlink()
    assert detonar.persistencia_windows(tmp_path) == []
    (inicio / "x.lnk").write_text("")
    assert any("Inicio" in m for m in detonar.persistencia_windows(tmp_path))


def test_traza_con_wine_empieza_en_el_programa_y_ve_los_senuelos_de_windows():
    h = detonar.HOME_FALSO
    login = f"{h}/{detonar.WIN_USER}/AppData/Local/Google/Chrome/User Data/Default/Login Data"
    traza = "\n".join([
        '1 execve("/usr/bin/bwrap", ["bwrap"], 0x0) = 0',
        f'2 openat(AT_FDCWD, "{login}", O_RDONLY) = -1 ENOENT',                       # antes de Wine: la jaula
        '3 execve("/usr/bin/wine", ["wine", "/tmp/muestra/x.exe"], 0x0 /* 9 vars */) = 0',
        '3 openat(AT_FDCWD, "/tmp/muestra/x.exe", O_RDONLY) = 5',
        f'4 openat(AT_FDCWD, "{login}", O_RDONLY) = 7',
    ])
    inf = detonar.analizar_traza(traza, h, "/tmp/muestra/x.exe")
    assert inf.veredicto == "malicioso" and any("secreto" in m for m in inf.motivos)
    assert detonar.se_ejecuto(traza, "/tmp/muestra/x.exe", wine=True)
    assert not detonar.se_ejecuto(traza, "/tmp/muestra/x.exe")       # sin Wine habría hecho falta un execve


def test_sin_wine_no_sale_limpio(tmp_path, monkeypatch):
    monkeypatch.setattr(detonar.shutil, "which", lambda n: None)
    inf = detonar.detonar(str(pe(tmp_path / "a.exe")), base_dir=tmp_path, run=lambda *a, **k: None)
    assert inf.veredicto == "sospechoso" and "Wine" in inf.nota


def test_con_wine_se_ejecuta_con_wine_y_en_el_aislamiento(tmp_path, monkeypatch):
    real_which = detonar.shutil.which
    monkeypatch.setattr(detonar.shutil, "which", lambda n: "/usr/bin/wine" if n == "wine" else real_which(n))
    vistos = []
    detonar.detonar(str(pe(tmp_path / "a.exe")), base_dir=tmp_path, run=lambda cmd, **k: vistos.append(cmd),
                    usar_vm=True)
    cmd = vistos[0]
    assert "--unshare-all" in cmd and "/usr/bin/wine" in cmd and "/tmp/muestra/a.exe" in cmd
    assert "qemu-system-x86_64" not in cmd                            # la VM de análisis no trae Wine
    assert cmd[cmd.index("WINEDLLOVERRIDES") + 1].endswith("winemenubuilder.exe=d")


# ---------------------------------------------------------------- consulta por socket
class Analizador:
    def __init__(self, veredicto="limpio"):
        self.veredicto, self.llamadas = veredicto, []

    def analyze(self, ruta, sha, uid, gid):
        self.llamadas.append((Path(ruta).read_bytes(), sha, uid, gid))
        return detonar.Informe(veredicto=self.veredicto, motivos=["prueba"], sha256=sha)


@pytest.fixture
def servidor(tmp_path):
    def crear(analizador, permitidos=None):
        c = consulta.Consulta(analizador, sock=tmp_path / "c.sock", det_dir=tmp_path / "det",
                              cargar=lambda: permitidos or {})
        c.start()
        return c
    yield crear


def test_revisa_los_bytes_exactos_con_la_identidad_del_usuario(tmp_path, servidor):
    a = Analizador("limpio")
    c = servidor(a)
    exe = pe(tmp_path / "juego.exe", b"contenido")
    avances = []
    r = consulta.pedir_revision(str(exe), al_avanzar=avances.append, sock=c.sock_path)
    c.stop()
    assert r["estado"] == "listo" and r["veredicto"] == "limpio" and avances
    datos, sha, uid, _gid = a.llamadas[0]
    assert datos == exe.read_bytes() and sha == r["sha256"]
    assert uid == (os.getuid() or 65534)                    # nunca como root
    assert not list((tmp_path / "det").iterdir())           # la copia se borra al terminar


def test_lo_ya_revisado_responde_al_instante(tmp_path, servidor):
    exe = pe(tmp_path / "a.exe")
    import hashlib
    sha = hashlib.sha256(exe.read_bytes()).hexdigest()
    a = Analizador()
    c = servidor(a, {sha: {"veredicto": "malicioso", "motivos": ["firma X"]}})
    r = consulta.pedir_revision(str(exe), sock=c.sock_path)
    c.stop()
    assert r["veredicto"] == "malicioso" and r["ya_revisado"] and not a.llamadas
    # un administrador permitió uno «sospechoso»: se abre; uno malicioso nunca se convierte en limpio
    c = servidor(a, {sha: {"veredicto": "sospechoso", "permitido_por_usuario": 1}})
    assert consulta.pedir_revision(str(exe), sock=c.sock_path)["veredicto"] == "limpio"
    c.stop()


def test_rechaza_pedidos_sin_archivo_o_que_no_son_archivos(tmp_path, servidor):
    c = servidor(Analizador())
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(c.sock_path))
        s.sendall(json.dumps({"accion": "revisar"}).encode() + b"\n")
        assert b"hace falta el archivo" in s.recv(4096)
    with pytest.raises(OSError):
        consulta.pedir_revision(str(tmp_path), sock=c.sock_path)   # una carpeta no se puede abrir así
    r_fd, w_fd = os.pipe()                                             # una tubería no es un archivo normal
    with socket.socket(socket.AF_UNIX) as s:
        s.connect(str(c.sock_path))
        import array
        s.sendmsg([b'{"accion": "revisar"}\n'], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [r_fd]))])
        assert b"no es un archivo normal" in s.recv(4096)
    os.close(r_fd)
    os.close(w_fd)
    c.stop()


def test_una_revision_por_usuario_a_la_vez(tmp_path, servidor):
    seguir = threading.Event()

    class Lento(Analizador):
        def analyze(self, *a):
            seguir.wait(10)
            return super().analyze(*a)
    c = servidor(Lento())
    exe = pe(tmp_path / "a.exe")
    primero = {}
    t = threading.Thread(target=lambda: primero.update(consulta.pedir_revision(str(exe), sock=c.sock_path)))
    t.start()
    import time
    for _ in range(100):
        if c._por_usuario:
            break
        time.sleep(0.02)
    r = consulta.pedir_revision(str(pe(tmp_path / "b.exe", b"x")), sock=c.sock_path)
    seguir.set()
    t.join(10)
    c.stop()
    assert r["estado"] == "error" and "otra revisión" in r["mensaje"]
    assert primero["veredicto"] == "limpio"
