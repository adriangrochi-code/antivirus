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
                                     writers=lambda uid=None: [(victim.pid, 10_000_000, "/home/ana/cifrador")])
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
