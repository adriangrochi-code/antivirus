import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo import behavior  # noqa: E402

LOG = """type=SYSCALL msg=audit(1731300000.123:501): arch=c000003e syscall=257 success=yes exit=3 a0=ffffff9c pid=4242 ppid=1 uid=1000 comm="robador" exe="/tmp/robador" key="secretos"
type=CWD msg=audit(1731300000.123:501): cwd="/home/ana"
type=PATH msg=audit(1731300000.123:501): item=0 name="/home/ana/.ssh/id_ed25519" inode=12 nametype=NORMAL
type=EOE msg=audit(1731300000.123:501):
type=SYSCALL msg=audit(1731300001.000:502): arch=c000003e syscall=257 success=yes pid=4300 uid=1000 comm="ssh" exe="/usr/bin/ssh" key="secretos"
type=PATH msg=audit(1731300001.000:502): item=0 name="/home/ana/.ssh/id_ed25519" nametype=NORMAL
type=EOE msg=audit(1731300001.000:502):
type=SYSCALL msg=audit(1731300002.000:503): arch=c000003e syscall=257 success=yes pid=4400 uid=1000 comm="x" exe="/home/ana/.local/x" key="persistencia"
type=PATH msg=audit(1731300002.000:503): item=1 name="/home/ana/.config/autostart/x.desktop" nametype=CREATE
type=EOE msg=audit(1731300002.000:503):
"""


def test_audit_rules_evaluation():
    ev = behavior.group_events(LOG.splitlines())
    a = behavior.evaluate(ev["501"])
    assert a and a.kind == "secretos" and a.severity == "high" and a.pid == 4242 and "id_ed25519" in a.detail
    assert behavior.evaluate(ev["502"]) is None                  # ssh leyendo su clave: legítimo
    p = behavior.evaluate(ev["503"])
    assert p and p.kind == "persistencia" and "autostart" in p.detail


def test_hex_encoded_names():
    f = behavior.parse_fields("type=PATH msg=audit(1.0:9): name=2F746D702F612062 nametype=NORMAL")
    assert f["name"] == "/tmp/a b"


def test_looks_encrypted():
    rnd = os.urandom(8192)
    assert behavior.looks_encrypted("a.pdf", rnd)
    assert not behavior.looks_encrypted("a.pdf", b"%PDF-1.7\n" + rnd)
    assert behavior.looks_encrypted("notas.txt", rnd)
    assert not behavior.looks_encrypted("notas.txt", ("hola mundo " * 800).encode())
    assert not behavior.looks_encrypted("foto.raw", rnd)            # tipo desconocido: sin opinión


def test_ransomware_guard_freezes_culprit(tmp_path):
    victim = subprocess.Popen(["sleep", "30"])
    alerts = []
    try:
        g = behavior.RansomwareGuard(threshold=5, window_s=10, on_alert=alerts.append,
                                     writers=lambda uid=None: [(victim.pid, 10_000_000,
                                                                os.readlink(f"/proc/{victim.pid}/exe"))])
        g.protect_root = os.geteuid() != 0
        for i in range(5):
            f = tmp_path / f"doc{i}.pdf"
            f.write_bytes(os.urandom(8192))
            g.file_changed(str(f), now=1000 + i)
        assert alerts and alerts[0].kind == "ransomware" and "CONGELADO" in alerts[0].action
        time.sleep(0.2)
        state = Path(f"/proc/{victim.pid}/stat").read_text().split()[2]
        assert state == "T"                                          # detenido (SIGSTOP)
    finally:
        victim.kill()
        victim.wait()


def test_normal_edits_do_not_trigger(tmp_path):
    alerts = []
    g = behavior.RansomwareGuard(threshold=3, on_alert=alerts.append, writers=lambda uid=None: [])
    for i in range(10):
        f = tmp_path / f"n{i}.txt"
        f.write_text("texto normal " * 200)
        g.file_changed(str(f), now=1000 + i)
    assert not alerts


def test_ransomware_guard_never_freezes_a_different_process(tmp_path):
    """Si entre el muestreo y la congelación el PID pasa a otro programa, solo se alerta."""
    victim = subprocess.Popen(["sleep", "30"])
    alerts = []
    try:
        g = behavior.RansomwareGuard(threshold=3, on_alert=alerts.append,
                                     writers=lambda uid=None: [(victim.pid, 1, "/usr/bin/otro-programa")])
        g.protect_root = False
        for i in range(3):
            f = tmp_path / f"d{i}.pdf"
            f.write_bytes(os.urandom(8192))
            g.file_changed(str(f), now=1000 + i)
        assert alerts and "CONGELADO" not in alerts[0].action
        assert Path(f"/proc/{victim.pid}/stat").read_text().split()[2] != "T"
    finally:
        victim.kill()
        victim.wait()


def test_ransomware_guard_ignores_fifos_and_symlinks(tmp_path):
    g = behavior.RansomwareGuard(threshold=1, on_alert=lambda a: None, writers=lambda uid=None: [])
    fifo = tmp_path / "x.pdf"
    os.mkfifo(fifo)
    assert g.file_changed(str(fifo)) is None                      # no se bloquea esperando a un escritor
    target = tmp_path / "real.pdf"
    target.write_bytes(os.urandom(4096))
    link = tmp_path / "enlace.pdf"
    link.symlink_to(target)
    assert g.file_changed(str(link)) is None


def test_ransomware_appended_extension_and_stl(tmp_path):
    cifrado = os.urandom(70000)
    f = tmp_path / "informe.pdf.locked"
    f.write_bytes(cifrado)
    assert behavior.looks_encrypted(str(f), cifrado)                  # antes: extensión desconocida → ignorado
    g = tmp_path / "foto.raw.xyz"
    g.write_bytes(cifrado)
    assert not behavior.looks_encrypted(str(g), cifrado)              # sin tipo conocido: no se opina
    stl = tmp_path / "pieza.stl"
    tri = 100                                                         # STL binario válido (datos aleatorios:
    datos = b"\0" * 80 + tri.to_bytes(4, "little") + os.urandom(50 * tri)   # entropía alta, como uno real)
    stl.write_bytes(datos)
    assert not behavior.looks_encrypted(str(stl), datos)              # tamaño coherente: es un STL, no cifrado
    malo = tmp_path / "pieza2.stl"
    malo.write_bytes(cifrado)
    assert behavior.looks_encrypted(str(malo), cifrado)               # antes: un .stl nunca podía marcarse


def _ev(exe, key, name="", auid="1000", serial="9"):
    log = (f'type=SYSCALL msg=audit(1.0:{serial}): arch=c000003e syscall=1 success=yes pid=77 auid={auid} '
           f'uid=0 exe="{exe}" key="{key}"\n')
    if name:
        log += f'type=PATH msg=audit(1.0:{serial}): item=0 name="{name}" nametype=CREATE\n'
    return behavior.evaluate(behavior.group_events(log.splitlines())[serial])


def test_new_detections_and_less_noise():
    assert _ev("/usr/bin/kmod", "modulos") is None                      # udev/kmod cargando drivers: normal
    assert _ev("/tmp/rk", "modulos").severity == "high"
    assert _ev("/usr/bin/strace", "ptrace", auid="4294967295") is None  # el propio análisis de Escudo
    assert _ev("/usr/bin/strace", "ptrace", auid="1000").kind == "ptrace"
    assert _ev("/usr/lib/systemd/systemd", "ebpf") is None
    a = _ev("/dev/shm/.x", "ebpf")
    assert a.kind == "ebpf" and a.severity == "high" and "rootkit" in a.detail
    assert _ev("/usr/bin/ssh", "ssh_claves", "/home/ana/.ssh/known_hosts") is None
    b = _ev("/tmp/x", "ssh_claves", "/home/ana/.ssh/authorized_keys")
    assert b.kind == "ssh_claves" and b.severity == "high"


def test_fileless_and_deleted_processes(tmp_path):
    proc = tmp_path / "proc"
    casos = {"10": "/memfd:carga (deleted)", "11": "/memfd:runc_cloned:/proc/self/exe (deleted)",
             "12": "/tmp/.x (deleted)", "13": "/usr/bin/firefox (deleted)", "14": "/usr/bin/bash"}
    for pid, exe in casos.items():
        (proc / pid).mkdir(parents=True)
        os.symlink(exe, proc / pid / "exe")
    vistos = set()
    al = {a.pid: a for a in behavior.procesos_sin_archivo(str(proc), vistos)}
    assert set(al) == {10, 12}                                     # runc, actualizaciones y normales: no
    assert al[10].kind == "sin_archivo" and al[10].severity == "high"
    assert al[12].kind == "binario_borrado"
    assert behavior.procesos_sin_archivo(str(proc), vistos) == []  # no se repite
